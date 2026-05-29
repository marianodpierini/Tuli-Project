import botocore
import unicodedata
from datetime import date, datetime, timedelta, timezone
from logging import Logger
from typing import Any, Dict, Optional
from langsmith import traceable, get_current_run_tree


from core.improved_context_classes import EnhancedUserContext

KEYWORDS_USER_CONTEXT = [
    "mis",
    "mi",
    "yo",
    "voy",
    "hice",
    "hago",
    "personales",
    "personal",
    "objetivos",
]


class BaseBedrockAgent:
    def __init__(
        self,
        logger: Logger,
        user_context: EnhancedUserContext,
        boto3_client: Any,
        agent_responses_feedback: Any,
        config: botocore.config.Config,
        session_id: str,
        session_local: Any,
        source: str,
        agent_id: str,
        agent_alias_id: str,
    ):
        self.logger = logger
        self.user_context = user_context
        self.boto3_client = boto3_client
        self.agent_responses_feedback = agent_responses_feedback
        self.config = config
        self.session_id = session_id
        self.session_local = session_local
        self.source = source
        self.agent_id = agent_id
        self.agent_alias_id = agent_alias_id

    def clean_input_text(self, text: str) -> str:
        text = "".join(filter(str.isprintable, text))
        return text

    def normalize_text(self, text: str) -> str:
        return unicodedata.normalize("NFKC", text)

    @traceable(run_type="tool", name="Bedrock Agent Runtime")
    def _invoke_bedrock_agent(
        self, last_message: str, new_q_id: Optional[int] = None
    ) -> str:
        params = {
            "agentId": self.agent_id,
            "agentAliasId": self.agent_alias_id,
            "sessionId": self.session_id,
            "inputText": last_message,
            "enableTrace": True,
            "sessionState": {
                "sessionAttributes": {
                    "suggestion_id": str(new_q_id) if new_q_id else ""
                }
            },
        }

        try:
            self.logger.info(f"Enviando solicitud a Bedrock con parámetros: {params}")
            response = self.boto3_client.invoke_agent(**params)

            assistant_response = ""
            event_stream = response.get("completion")

            total_input_tokens = 0
            total_output_tokens = 0
            if isinstance(event_stream, botocore.eventstream.EventStream):
                for event in event_stream:
                    if "chunk" in event:
                        try:
                            chunk_data = event["chunk"]["bytes"].decode("utf-8")
                            assistant_response += chunk_data
                        except Exception as decode_error:
                            self.logger.error(
                                f"Error decodificando chunk: {decode_error}"
                            )

                    if "trace" in event:
                        trace_data = (
                            event["trace"]
                            .get("trace", {})
                            .get("orchestrationTrace", {})
                        )
                        if "modelInvocationOutput" in trace_data:
                            self.user_context.update_use_tokens(
                                self.source, trace_data=trace_data
                            )
                            
                            # Acumular tokens para LangSmith
                            metadata = trace_data.get("modelInvocationOutput", {}).get("metadata", {})
                            usage = metadata.get("usage", {})
                            total_input_tokens += usage.get("inputTokens", 0)
                            total_output_tokens += usage.get("outputTokens", 0)

                run_tree = get_current_run_tree()
                if run_tree:
                    run_tree.end(
                        outputs={"answer": assistant_response.strip()},
                        usage_metadata={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}
                    )
            else:
                self.logger.error(
                    f"Tipo inesperado en 'completion': {type(event_stream)}"
                )

            self.logger.info(
                f"[AGENT RESPONSE] Respuesta del agente: {assistant_response.strip()}"
            )
            # Uncomment and implement agent_responses_feedback.put_item if needed
            return assistant_response.strip()

        except Exception as e:
            self.logger.error(
                f"Error al invocar agente Bedrock ({self.agent_alias_id}): {str(e)}"
            )
            raise  # Re-raise to be handled by LanggraphManager or API handler
