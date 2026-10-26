"""Exercise auto-titling through a running EventService at the LLM boundary."""

import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import litellm
import pytest
from litellm.exceptions import AuthenticationError
from pydantic import SecretStr

from openhands.agent_server.conversation_service import AutoTitleSubscriber
from openhands.agent_server.event_service import EventService
from openhands.agent_server.models import StoredConversation
from openhands.agent_server.persistence.store import get_llm_profile_store
from openhands.agent_server.pub_sub import Subscriber
from openhands.sdk import LLM, Agent, Message, TextContent
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.security.confirmation_policy import NeverConfirm
from openhands.sdk.settings import ACPAgentSettings
from openhands.sdk.utils.cipher import Cipher
from openhands.sdk.workspace import LocalWorkspace


@pytest.mark.parametrize(
    "profile,agent_fails,acp,expected_calls,expected_title,error_count",
    [
        ("working", False, False, ["profile"], "Profile title", 0),
        ("valid", False, False, ["profile", "agent"], "Agent title", 0),
        ("valid", True, False, ["profile", "agent"], "Fix the login bug", 1),
        (None, False, False, ["agent"], "Agent title", 0),
        (None, True, False, ["agent"], "Fix the login bug", 1),
        ("missing", False, False, ["agent"], "Agent title", 0),
        ("corrupt", False, False, ["agent"], "Agent title", 0),
        (None, False, True, [], "Fix the login bug", 0),
        ("valid", False, True, ["profile"], "Fix the login bug", 1),
    ],
)
async def test_auto_title_fallback(
    tmp_path, profile, agent_fails, acp, expected_calls, expected_title, error_count
):
    cipher = Cipher("title-fallback-test")
    store = get_llm_profile_store()
    if profile in {"valid", "working"}:
        store.save(
            profile,
            LLM(
                model="openai/gpt-4o-mini",
                api_key=SecretStr("profile-key"),
                base_url="http://profile.invalid/v1",
                num_retries=0,
            ),
            include_secrets=True,
            cipher=cipher,
        )
    elif profile == "corrupt":
        store.save(profile, LLM(model="gpt-4o-mini"), cipher=cipher)
        # The persisted profile exists but cannot be validated.
        for path in store.base_dir.glob("*.json"):
            path.write_text("{}")

    agent_llm = (
        ACPAgentSettings().create_agent().llm
        if acp
        else LLM(
            model="openai/gpt-4o",
            api_key=SecretStr("agent-key"),
            base_url="http://agent.invalid/v1",
            num_retries=0,
        )
    )
    service = EventService(
        stored=StoredConversation(
            id=uuid4(),
            workspace=LocalWorkspace(working_dir=str(tmp_path / "workspace")),
            confirmation_policy=NeverConfirm(),
            title_llm_profile=profile,
            prompt="Summarize {conversation_content} in {max_length} characters.",
        ),
        agent=Agent(llm=agent_llm, tools=[]),
        conversations_dir=tmp_path / "conversations",
        cipher=cipher,
    )
    calls = []
    events = []

    def completion(**kwargs):
        name = "profile" if kwargs["api_key"] == "profile-key" else "agent"
        calls.append(name)
        assert kwargs["api_base"] == f"http://{name}.invalid/v1"
        assert kwargs["messages"][0]["role"] == "system"
        assert "Summarize Fix the login bug in 50 characters." in str(
            kwargs["messages"]
        )
        if (name == "profile" and profile != "working") or agent_fails:
            raise AuthenticationError(
                "Invalid proxy server token passed",
                model=kwargs["model"],
                llm_provider="openai",
            )
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "Profile title"
                        if name == "profile"
                        else "Agent title",
                    }
                }
            ],
        )

    async def agent_completion(**kwargs):
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "finish-1",
                                "type": "function",
                                "function": {
                                    "name": "finish",
                                    "arguments": json.dumps(
                                        {"message": "Task complete"}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
        )

    async def collect(event):
        events.append(event)

    with (
        patch("openhands.sdk.llm.llm.litellm_completion", side_effect=completion),
        patch(
            "openhands.sdk.llm.llm.litellm_acompletion", side_effect=agent_completion
        ),
    ):
        await service.start()
        try:
            collector = AsyncMock(spec=Subscriber)
            collector.side_effect = collect
            await service.subscribe_to_events(collector)
            await service.subscribe_to_events(AutoTitleSubscriber(service))
            await service.send_message(
                Message(role="user", content=[TextContent(text="Fix the login bug")]),
                run=not acp,
            )
            if not acp:
                assert await service.wait_for_run_completion(timeout=10) == (
                    ConversationExecutionStatus.FINISHED
                )
            async with asyncio.timeout(10):
                while service.stored.title is None:
                    await asyncio.sleep(0.01)
            assert service._callback_wrapper is not None
            await asyncio.to_thread(service._callback_wrapper.wait_for_pending)
            assert service.stored.title == expected_title
            assert (
                json.loads((service.conversation_dir / "meta.json").read_text())[
                    "title"
                ]
                == expected_title
            )
            assert calls == expected_calls
            errors = [e for e in events if isinstance(e, ConversationErrorEvent)]
            assert len(errors) == error_count
            for error in errors:
                assert error.code == "TitleGenerationError"
                assert error.classification is not None
                assert error.classification.kind == "unknown"
                assert error.classification.user_action == "none"
                assert error.classification.retryable is False
            assert not any(
                isinstance(e, ConversationStateUpdateEvent)
                and e.key == "execution_status"
                and e.value == "error"
                for e in events
            )
            service._publish_error_event_sync(
                AuthenticationError(
                    "Invalid proxy server token passed",
                    model="gpt-4o",
                    llm_provider="openai",
                )
            )
            assert service._callback_wrapper is not None
            await asyncio.to_thread(service._callback_wrapper.wait_for_pending)
            assert events[-1].classification.kind == "auth"
            assert events[-1].classification.user_action == "settings"
        finally:
            await service.close()
