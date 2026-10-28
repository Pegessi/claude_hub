"""Authenticated question listing, exact answer intake and explicit remirroring."""

from fastapi import APIRouter, Depends, HTTPException

from ....auth.dependencies import get_current_user
from ....models import User
from ....models.chat_message import QuestionAnswerCreate
from ....services.agent_stream.question_answers import answers
from ....services.chat_message_inbox import InboxError


def register_question_routes(router: APIRouter) -> None:
    @router.get("/tabs/{tab_id}/questions")
    async def list_questions(tab_id: str, user: User = Depends(get_current_user)) -> dict:
        from ... import agent_stream as stream

        stream._terminal_tab_session_or_404(tab_id)
        try:
            inbox = answers.inbox()
            return {
                "questions": [q.public() for q in inbox.questions.values() if q.tab_id == tab_id]
            }
        except Exception:
            raise HTTPException(503, "Question inbox unavailable") from None

    @router.get("/sessions/{managed_session_id}/questions")
    async def list_managed_questions(
        managed_session_id: str, user: User = Depends(get_current_user)
    ) -> dict:
        from ... import agent_stream as stream

        session = stream.workspace_manager.sessions.get(managed_session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        async with stream._agent_stream_bindings.managed_stream_interaction(
            session, require_chat=True
        ):
            return {
                "questions": [
                    q.public()
                    for q in answers.inbox().questions.values()
                    if q.session_id == managed_session_id
                ]
            }

    @router.post("/tabs/{tab_id}/questions/{question_ref}/answers", status_code=202)
    async def answer_question(
        tab_id: str,
        question_ref: str,
        payload: QuestionAnswerCreate,
        user: User = Depends(get_current_user),
    ) -> dict:
        from ... import agent_stream as stream

        stream._terminal_tab_session_or_404(tab_id)
        try:
            q = answers.inbox().questions.get(question_ref)
            if q is None or q.tab_id != tab_id:
                raise InboxError(404, "Question not found")
            return (await answers.admit(q, user, payload)).model_dump(mode="json")
        except InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @router.post("/tabs/{tab_id}/questions/{question_ref}/mirror")
    async def remirror(
        tab_id: str, question_ref: str, user: User = Depends(get_current_user)
    ) -> dict:
        from ... import agent_stream as stream
        from ..feishu_bot.card_actions import mirror_question

        stream._terminal_tab_session_or_404(tab_id)
        q = answers.inbox().questions.get(question_ref)
        if q is None or q.tab_id != tab_id:
            raise HTTPException(404, "Question not found")
        try:
            return {"sent": await mirror_question(q, remirror=True)}
        except InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
