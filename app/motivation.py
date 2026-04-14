from __future__ import annotations

import logging
from datetime import date

import httpx

from app.config import AppConfig
from app.constants import MOTIVATION_QUOTES
from app.messages import format_motivation_message
from app.repository import FirestoreRepository

LOGGER = logging.getLogger(__name__)


class MotivationService:
    def __init__(self, config: AppConfig, repository: FirestoreRepository) -> None:
        self.config = config
        self.repository = repository

    async def get_daily_motivation_message(self, today: date, day_name: str, tasks: list[dict]) -> str:
        cached = self.repository.get_daily_motivation(today)
        if cached and cached.get("text"):
            return format_motivation_message(cached["text"])

        text, source, model_used = await self._resolve_motivation_text(today=today, day_name=day_name, tasks=tasks)
        self.repository.save_daily_motivation(
            scheduled_date=today,
            weekday=day_name,
            task_items=tasks,
            text=text,
            source=source,
            model_used=model_used,
        )
        return format_motivation_message(text)

    async def _resolve_motivation_text(
        self,
        today: date,
        day_name: str,
        tasks: list[dict],
    ) -> tuple[str, str, str | None]:
        if self.config.ollama_api_key:
            try:
                text, model_used = await self._generate_with_ollama(day_name=day_name, tasks=tasks)
                if text:
                    return text, "ollama", model_used
            except Exception:
                LOGGER.exception("Falling back to built-in motivation for %s after Ollama request failed.", today.isoformat())
        else:
            LOGGER.info("OLLAMA_API_KEY is not set. Using built-in motivation text.")

        return self._fallback_text(today=today, day_name=day_name, tasks=tasks), "fallback", None

    async def _generate_with_ollama(self, day_name: str, tasks: list[dict]) -> tuple[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.ollama_api_key:
            headers["Authorization"] = f"Bearer {self.config.ollama_api_key}"

        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an elite but supportive gym coach. "
                        "Write a short daily motivation message based on the provided workout plan. "
                        "Requirements: 2 to 4 sentences, under 90 words, plain text only, no emojis, no hashtags, "
                        "no markdown, and make it feel specific to the session."
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_prompt(day_name=day_name, tasks=tasks),
                },
            ],
            "stream": False,
        }

        async with httpx.AsyncClient(
            base_url=self.config.ollama_api_base_url,
            headers=headers,
            timeout=self.config.ollama_timeout_seconds,
        ) as client:
            last_error: str | None = None
            for model_name in self._candidate_models():
                response = await client.post(
                    "chat",
                    json={
                        **payload,
                        "model": model_name,
                    },
                )

                if response.status_code >= 400:
                    last_error = f"{response.status_code} {response.text}"
                    continue

                data = response.json()
                text = ((data.get("message") or {}).get("content") or "").strip()
                if text:
                    return self._clean_text(text), model_name
                last_error = "Ollama response did not include message.content."

        raise RuntimeError(last_error or "Ollama motivation generation failed.")

    def _candidate_models(self) -> list[str]:
        candidates = [self.config.ollama_model]
        normalized_base = self.config.ollama_api_base_url.lower()
        if normalized_base.startswith("https://ollama.com") and self.config.ollama_model.endswith(":cloud"):
            candidates.append(self.config.ollama_model.removesuffix(":cloud"))

        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _build_prompt(self, day_name: str, tasks: list[dict]) -> str:
        if not tasks:
            return (
                f"Day: {day_name}\n"
                "Plan: Recovery / Rest Day\n"
                "Write motivation that respects recovery, consistency, and discipline."
            )

        lines = [f"Day: {day_name}", "Plan:"]
        for task in tasks:
            details = task.get("details", "").strip()
            if details:
                lines.append(f"- {task['title']}: {details}")
            else:
                lines.append(f"- {task['title']}")

        lines.append("Write motivation that matches this training focus and pushes the athlete to complete the session.")
        return "\n".join(lines)

    def _clean_text(self, text: str) -> str:
        cleaned_lines = [line.strip(" -\t") for line in text.splitlines() if line.strip()]
        cleaned = " ".join(cleaned_lines).strip()
        return cleaned[:500]

    def _fallback_text(self, today: date, day_name: str, tasks: list[dict]) -> str:
        if tasks:
            titles = ", ".join(task["title"] for task in tasks[:2])
            if len(tasks) > 2:
                titles = f"{titles}, and more"
            return (
                f"{MOTIVATION_QUOTES[today.toordinal() % len(MOTIVATION_QUOTES)]} "
                f"Today's {day_name} focus is {titles}. Lock in and finish the work with clean form."
            )

        return (
            "Recovery is part of the program. Use today to reset, refuel, and protect the streak by showing up strong "
            "for the next session."
        )
