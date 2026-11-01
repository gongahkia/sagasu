"""Private owner notifications for a login initiated through Sagasu."""

import os


def owner_auth_notifier(bot, *, render=None):
    async def notify(message):
        # Always use the configured user's private chat, even for group commands.
        from .fbs_client import AuthenticationRequired

        owner = os.getenv("TELEGRAM_OWNER_ID", "")
        if not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
            raise AuthenticationRequired(
                "Set TELEGRAM_OWNER_ID before requesting remote MFA approval."
            )
        try:
            if render is not None:
                await render(int(owner), f"🔐 {message}")
                return
            await bot.send_message(
                chat_id=int(owner), text=f"🔐 {message}", parse_mode=None
            )
        except Exception:
            raise AuthenticationRequired(
                "Could not deliver the private MFA prompt. Open the bot's private chat, send /start, then retry."
            ) from None

    return notify
