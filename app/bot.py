from __future__ import annotations

import logging
from datetime import date, datetime

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

from app.config import AppConfig
from app.firebase import initialize_firestore
from app.messages import (
    build_help_text,
    build_stats_message,
    build_today_message,
    build_weekly_plan,
)
from app.motivation import MotivationService
from app.parsers import parse_edit_day_input, parse_full_routine_input
from app.repository import FirestoreRepository, normalize_weekday

LOGGER = logging.getLogger(__name__)


class GymMotivationBot:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = initialize_firestore(config)
        self.repository = FirestoreRepository(
            db=self.db,
            default_admin_id=config.admin_telegram_id,
            default_reminder_time=config.reminder_time_text,
        )
        self.repository.initialize_defaults()
        self.motivation_service = MotivationService(config=config, repository=self.repository)
        self.application = self._build_application()
        self.daily_job_name = "daily-gym-broadcast"

    def _build_application(self) -> Application:
        application = Application.builder().token(self.config.telegram_bot_token).post_init(self._post_init).build()

        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("plan", self.plan_command))
        application.add_handler(CommandHandler("today", self.today_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("deregister", self.deregister_command))
        application.add_handler(CommandHandler("tasks", self.tasks_command))
        application.add_handler(CommandHandler("addtask", self.add_task_command))
        application.add_handler(CommandHandler("editday", self.edit_day_command))
        application.add_handler(CommandHandler("setroutine", self.set_routine_command))
        application.add_handler(CommandHandler("deletetask", self.delete_task_command))
        application.add_handler(CommandHandler("clearweekday", self.clear_weekday_command))
        application.add_handler(CommandHandler("settime", self.set_time_command))
        application.add_handler(CommandHandler("broadcastplan", self.broadcast_plan_command))
        application.add_handler(PollAnswerHandler(self.poll_answer_handler))
        application.add_handler(MessageHandler(filters.COMMAND, self.unknown_command))
        return application

    async def _post_init(self, application: Application) -> None:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Register and view the current workout plan"),
                BotCommand("plan", "Show the weekly workout plan"),
                BotCommand("today", "Show today's workout and check-in poll"),
                BotCommand("stats", "Show your streak and progress"),
                BotCommand("deregister", "Leave the routine and stop notifications"),
                BotCommand("help", "Show the help menu"),
            ]
        )
        self._reschedule_daily_job()

    def _local_today(self) -> date:
        return datetime.now(self.config.timezone).date()

    def _reschedule_daily_job(self) -> None:
        for job in self.application.job_queue.get_jobs_by_name(self.daily_job_name):
            job.schedule_removal()

        schedule_settings = self.repository.get_schedule_settings()
        hour_text, minute_text = schedule_settings.reminder_time.split(":", maxsplit=1)
        reminder_time = self.config.reminder_time.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            tzinfo=self.config.timezone,
        )
        self.application.job_queue.run_daily(
            self.daily_broadcast_job,
            time=reminder_time,
            name=self.daily_job_name,
        )
        LOGGER.info("Daily reminder scheduled at %s %s", schedule_settings.reminder_time, self.config.bot_timezone_name)

    def run(self) -> None:
        LOGGER.info("Starting bot polling loop.")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_chat or not update.message:
            return

        user_data, promoted = self.repository.ensure_user(
            telegram_user=update.effective_user,
            chat_id=update.effective_chat.id,
            joined_on=self._local_today(),
        )
        is_admin = bool(user_data.get("is_admin"))

        welcome_lines = [
            f"<b>Welcome to {self.config.bot_name}</b>",
            "Your weekly routine lives here, your check-ins stay in Firestore, and your streak updates automatically.",
        ]
        if promoted:
            welcome_lines.append("You have been set as the first admin for this bot.")

        await update.message.reply_text(
            "\n".join(welcome_lines),
            parse_mode=ParseMode.HTML,
        )

        await update.message.reply_text(
            build_weekly_plan(self.repository.get_weekly_plan(), show_ids=is_admin),
            parse_mode=ParseMode.HTML,
        )

        await update.message.reply_text(
            build_help_text(is_admin),
            parse_mode=ParseMode.HTML,
        )

        await self._send_today_flow(
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id,
            bot=context.bot,
            force_poll=False,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.message:
            return
        await update.message.reply_text(
            build_help_text(self.repository.is_admin(update.effective_user.id)),
            parse_mode=ParseMode.HTML,
        )

    async def plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.message:
            return
        if not await self._require_active_member(update):
            return
        await update.message.reply_text(
            build_weekly_plan(
                self.repository.get_weekly_plan(),
                show_ids=self.repository.is_admin(update.effective_user.id),
            ),
            parse_mode=ParseMode.HTML,
        )

    async def today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_chat or not update.message:
            return
        if not await self._require_active_member(update):
            return
        await self._send_today_flow(
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id,
            bot=context.bot,
            force_poll=False,
        )

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.message:
            return
        if not await self._require_active_member(update):
            return

        stats = self.repository.get_user_stats(update.effective_user.id, today=self._local_today())
        await update.message.reply_text(build_stats_message(stats), parse_mode=ParseMode.HTML)

    async def deregister_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.message:
            return

        self.repository.deregister_user(update.effective_user.id)
        await update.message.reply_text(
            "You have been removed from the weekly routine and daily notifications. Send /start any time to join again."
        )

    async def tasks_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.message:
            return
        await update.message.reply_text(
            build_weekly_plan(self.repository.get_weekly_plan(), show_ids=True),
            parse_mode=ParseMode.HTML,
        )

    async def add_task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.effective_user or not update.message:
            return

        raw = update.message.text.partition(" ")[2]
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 3 or not all(parts[:3]):
            await update.message.reply_text(
                "Use this format:\n/addtask Monday | Push Day | Bench Press, Incline Press, Dips"
            )
            return

        weekday, title, details = parts[:3]
        try:
            task = self.repository.create_task(
                weekday=weekday,
                title=title,
                details=details,
                created_by=update.effective_user.id,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return

        await update.message.reply_text(
            f"Saved {task['title']} for {task['weekday']}. Broadcasting the updated weekly plan now."
        )
        self.repository.delete_daily_motivation(self._local_today())
        await self._broadcast_plan(context.bot)

    async def edit_day_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.effective_user or not update.message:
            return

        raw = update.message.text.partition(" ")[2]
        try:
            weekday, items = parse_edit_day_input(raw)
            created = self.repository.replace_weekday_tasks(
                weekday=weekday,
                items=items,
                created_by=update.effective_user.id,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return

        normalized = normalize_weekday(weekday)
        await update.message.reply_text(
            f"Updated {normalized} with {len(created)} plan item(s). Broadcasting the refreshed weekly plan now."
        )
        self.repository.delete_daily_motivation(self._local_today())
        await self._broadcast_plan(context.bot)

    async def set_routine_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.effective_user or not update.message:
            return

        raw = update.message.text.partition(" ")[2]
        try:
            routine = parse_full_routine_input(raw)
            created = self.repository.replace_full_routine(
                routine=routine,
                created_by=update.effective_user.id,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return

        total_items = sum(len(items) for items in created.values())
        total_days = len(created)
        await update.message.reply_text(
            f"Replaced the full weekly routine with {total_items} plan item(s) across {total_days} training day(s). "
            "Broadcasting the refreshed weekly plan now."
        )
        self.repository.delete_daily_motivation(self._local_today())
        await self._broadcast_plan(context.bot)

    async def delete_task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.message:
            return

        task_id = update.message.text.partition(" ")[2].strip()
        if not task_id:
            await update.message.reply_text("Use /deletetask TASK_ID")
            return

        deleted_task = self.repository.delete_task(task_id)
        if not deleted_task:
            await update.message.reply_text("Task ID not found.")
            return

        await update.message.reply_text("Task deleted. Broadcasting the refreshed weekly plan now.")
        self.repository.delete_daily_motivation(self._local_today())
        await self._broadcast_plan(context.bot)

    async def clear_weekday_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.message:
            return

        weekday = update.message.text.partition(" ")[2].strip()
        if not weekday:
            await update.message.reply_text("Use /clearweekday Monday")
            return

        try:
            normalized = normalize_weekday(weekday)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return

        deleted = self.repository.clear_weekday(normalized)
        await update.message.reply_text(
            f"Removed {deleted} task(s) from {normalized}. Broadcasting the refreshed weekly plan now."
        )
        self.repository.delete_daily_motivation(self._local_today())
        await self._broadcast_plan(context.bot)

    async def set_time_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        if not update.message:
            return

        value = update.message.text.partition(" ")[2].strip()
        try:
            hour = int(value.split(":", maxsplit=1)[0])
            minute = int(value.split(":", maxsplit=1)[1])
            validated = self.config.reminder_time.replace(hour=hour, minute=minute)
        except (IndexError, ValueError):
            await update.message.reply_text("Use /settime HH:MM with a valid 24-hour time.")
            return

        formatted = f"{validated.hour:02d}:{validated.minute:02d}"
        self.repository.set_reminder_time(formatted)
        self._reschedule_daily_job()
        await update.message.reply_text(
            f"Daily reminder time updated to {formatted} ({self.config.bot_timezone_name})."
        )

    async def broadcast_plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        await self._broadcast_plan(context.bot)
        if update.message:
            await update.message.reply_text("Broadcast finished.")

    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text("Unknown command. Use /help to see what the bot can do.")

    async def poll_answer_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.poll_answer:
            return

        try:
            poll_data, summary = self.repository.record_poll_answer(
                poll_id=update.poll_answer.poll_id,
                option_ids=update.poll_answer.option_ids,
                today=self._local_today(),
            )
        except ValueError:
            LOGGER.warning("Received answer for unknown poll %s", update.poll_answer.poll_id)
            return

        status = "completed" if update.poll_answer.option_ids[:1] == [0] else "skipped"
        message = (
            f"Check-in saved for {poll_data['scheduled_date']}: <b>{status.title()}</b>\n"
            f"Current streak: <b>{summary.current_streak}</b>\n"
            f"Longest streak: <b>{summary.longest_streak}</b>"
        )

        try:
            await context.bot.send_message(
                chat_id=poll_data["chat_id"],
                text=message,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            LOGGER.exception("Failed to send poll answer confirmation to user %s", poll_data["user_id"])

    async def daily_broadcast_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for user in self.repository.list_active_users():
            await self._send_today_flow(
                chat_id=user["chat_id"],
                user_id=user["telegram_id"],
                bot=context.bot,
                force_poll=False,
            )

    async def _send_today_flow(self, chat_id: int, user_id: int, bot, force_poll: bool) -> None:
        today = self._local_today()
        day_name = today.strftime("%A")
        tasks = self.repository.get_tasks_for_day(day_name)
        motivation = await self.motivation_service.get_daily_motivation_message(
            today=today,
            day_name=day_name,
            tasks=tasks,
        )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=build_today_message(day_name, tasks, motivation),
                parse_mode=ParseMode.HTML,
            )
        except Forbidden:
            self.repository.mark_user_inactive(user_id)
            return
        except TelegramError:
            LOGGER.exception("Failed to send routine message to user %s", user_id)
            return

        if not tasks:
            return

        existing = self.repository.get_checkin(user_id, today)
        if existing and existing.get("poll_id") and not force_poll:
            return

        try:
            poll_message = await bot.send_poll(
                chat_id=chat_id,
                question=f"Did you complete your {day_name} workout?",
                options=["Completed", "Skipped"],
                is_anonymous=False,
                allows_multiple_answers=False,
            )
            self.repository.upsert_poll_dispatch(
                user_id=user_id,
                chat_id=chat_id,
                scheduled_date=today,
                task_items=tasks,
                poll_id=poll_message.poll.id,
                message_id=poll_message.message_id,
            )
        except Forbidden:
            self.repository.mark_user_inactive(user_id)
        except TelegramError:
            LOGGER.exception("Failed to send poll to user %s", user_id)

    async def _broadcast_plan(self, bot) -> None:
        plan_message = build_weekly_plan(self.repository.get_weekly_plan(), show_ids=False)
        for user in self.repository.list_active_users():
            try:
                await bot.send_message(
                    chat_id=user["chat_id"],
                    text=plan_message,
                    parse_mode=ParseMode.HTML,
                )
            except Forbidden:
                self.repository.mark_user_inactive(user["telegram_id"])
            except TelegramError:
                LOGGER.exception("Failed to broadcast weekly plan to user %s", user["telegram_id"])

    async def _require_admin(self, update: Update) -> bool:
        if not update.effective_user or not update.message:
            return False
        if not self.repository.is_active_user(update.effective_user.id):
            await update.message.reply_text("You are currently deregistered. Send /start first to rejoin the routine.")
            return False
        if self.repository.is_admin(update.effective_user.id):
            return True

        await update.message.reply_text("Only the admin can manage the weekly routine.")
        return False

    async def _require_active_member(self, update: Update) -> bool:
        if not update.effective_user or not update.message:
            return False
        if self.repository.is_active_user(update.effective_user.id):
            return True

        await update.message.reply_text("You are currently deregistered. Send /start to join the routine again.")
        return False
