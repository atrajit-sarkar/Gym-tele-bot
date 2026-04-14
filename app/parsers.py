from __future__ import annotations


def parse_edit_day_input(raw: str) -> tuple[str, list[tuple[str, str]]]:
    text = raw.strip()
    if not text:
        raise ValueError(
            "Use this format:\n"
            "/editday Monday | Push Day | Bench Press, Incline Press ;; Core | Planks, Leg Raises"
        )

    weekday_text, separator, remainder = text.partition("|")
    if not separator:
        raise ValueError(
            "Use this format:\n"
            "/editday Monday | Push Day | Bench Press, Incline Press ;; Core | Planks, Leg Raises"
        )

    weekday = weekday_text.strip()
    segments = [segment.strip() for segment in remainder.split(";;") if segment.strip()]
    if not segments:
        raise ValueError(
            "Add at least one plan item after the weekday. Example:\n"
            "/editday Monday | Push Day | Bench Press, Incline Press"
        )

    items: list[tuple[str, str]] = []
    for segment in segments:
        title, details = _parse_day_segment(segment)
        items.append((title, details))

    return weekday, items


def parse_full_routine_input(raw: str) -> dict[str, list[tuple[str, str]]]:
    text = raw.strip()
    if not text:
        raise ValueError(
            "Use one line per day like this:\n"
            "/setroutine Monday | Push Day | Bench Press, Incline Press ;; Core | Planks\n"
            "Tuesday | Pull Day | Rows, Pull Ups"
        )

    routine: dict[str, list[tuple[str, str]]] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(
            "Use one line per day like this:\n"
            "/setroutine Monday | Push Day | Bench Press, Incline Press ;; Core | Planks\n"
            "Tuesday | Pull Day | Rows, Pull Ups"
        )

    seen_weekdays: set[str] = set()
    for line in lines:
        weekday, items = parse_edit_day_input(line)
        normalized_weekday_key = weekday.strip().lower()
        if normalized_weekday_key in seen_weekdays:
            raise ValueError(f"Duplicate weekday found in full routine: {weekday}")
        seen_weekdays.add(normalized_weekday_key)
        routine[weekday] = items

    return routine


def _parse_day_segment(segment: str) -> tuple[str, str]:
    parts = [part.strip() for part in segment.split("|", maxsplit=1)]
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "Each day item must look like: Title | Details\n"
            "Example: Push Day | Bench Press, Incline Press"
        )

    return parts[0], parts[1]
