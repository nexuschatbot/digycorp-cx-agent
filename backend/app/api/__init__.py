"""HTTP routes — thin wrappers over the services/agent (AD-I). Chat is SSE."""

from __future__ import annotations

from app.api.bookings import router as bookings_router
from app.api.call import router as call_router
from app.api.chat import router as chat_router
from app.api.console import router as console_router
from app.api.conversation import router as conversation_router
from app.api.feedback import router as feedback_router
from app.api.brochure import router as brochure_router
from app.api.insights import router as insights_router
from app.api.intents import router as intents_router
from app.api.speech import router as speech_router
from app.api.vehicles import router as vehicles_router
from app.api.voice import router as voice_router
from app.api.whatsapp import router as whatsapp_router

__all__ = [
    "bookings_router",
    "call_router",
    "chat_router",
    "console_router",
    "conversation_router",
    "feedback_router",
    "brochure_router",
    "insights_router",
    "intents_router",
    "speech_router",
    "vehicles_router",
    "voice_router",
    "whatsapp_router",
]
