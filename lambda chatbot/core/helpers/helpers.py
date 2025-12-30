from typing import List
import os
import json
import boto3
from urllib.parse import parse_qs
from core.database.models import SuggestedQuestions
from sqlalchemy import any_, and_, text, or_

from core.database.db import SessionLocal

CONTEXT_DEPENDENT_PHRASES = {
        "y después", "y ahora", "qué más", "seguí", "continúa",
        "cual de esos", "ese", "esa", "eso", "esas", "esos",
        "ahora", "después", "también", "además"
    }


def normalize_event(event):
    enriched_event = event
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")

    if http_method == "POST":
        headers = event.get("headers", {})
        raw_body = event.get("body", "")
        is_base64 = event.get("isBase64Encoded", False)

        if is_base64 and isinstance(raw_body, str):
                import base64
                raw_body = base64.b64decode(raw_body).decode("utf-8")

        if headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            params = parse_qs(raw_body)

            message = params.get("Body", [""])[0]
            from_number = params.get("From", [""])[0]
            to_number = params.get("To", [""])[0]

            enriched_event = {
                "resource": "/webhooks/whatsapp",
                "path": "/webhooks/whatsapp",
                "httpMethod": "POST",
                "headers": headers,
                "multiValueHeaders": {k: [v] if not isinstance(v, list) else v for k, v in headers.items()},
                "queryStringParameters": event.get("queryStringParameters"),
                "multiValueQueryStringParameters": event.get("multiValueQueryStringParameters"),
                "pathParameters": event.get("pathParameters"),
                "stageVariables": event.get("stageVariables"),
                "requestContext": event.get("requestContext", {}),
                "body": json.dumps({
                    "from": from_number,
                    "to": to_number,
                    "message": message,
                    "raw": params
                }),
                "isBase64Encoded": False,
                "source": "whatsapp"
            }
        
        elif path.endswith("/google"):
            body_json = json.loads(raw_body)

            message = body_json.get("text", "")
            space_name = body_json.get("space", "")
            thread_name = body_json.get("thread", "")

            sender = (
                body_json.get("rawEvent", {})
                    .get("message", {})
                    .get("sender", {})
            )
            sender_email = sender.get("email", "")
            sender_name = sender.get("displayName", "")


            enriched_event = {
                "resource": "/webhooks/google",
                "path": "/webhooks/google",
                "httpMethod": "POST",
                "headers": headers,
                "requestContext": event.get("requestContext", {}),
                "body": json.dumps({
                    "text": message,
                    "name": sender_name,
                    "email": sender_email,
                    "space_name": space_name,
                    "thread_name": thread_name,
                    "raw": body_json,
                }),
                "isBase64Encoded": False,
                "source": "google_chat"
            }

            print(enriched_event)
    
    return enriched_event


def valite_existing_response(session_id: str, keywords: List[str], user_input: str, boto_config):

        conditions = [SuggestedQuestions.keywords.ilike(f"%{kw}%") for kw in keywords]

        client = boto3.client('bedrock-agent-runtime', region_name='us-east-1', config=boto_config)

        with SessionLocal() as session:
            existing_question = (
                session.query(SuggestedQuestions)
                .filter(SuggestedQuestions.activa.is_(True), and_(*conditions))
                .order_by(SuggestedQuestions.prioridad)
                .first()
            )

            if existing_question and existing_question.sql_query is not None:
                sql_query = existing_question.sql_query
                query_results = session.execute(text(sql_query))

                query_result_dicts = [dict(row._mapping) for row in query_results]


                input_text = f"""
                    El usuario preguntó: "{user_input}"

                    La consulta SQL asociada (ID {existing_question.id}) devolvió estos resultados:
                    {json.dumps(query_result_dicts, ensure_ascii=False, indent=2)}

                    Por favor responde al usuario en lenguaje natural, breve y clara,
                    usando los resultados de la consulta.
                    """

                params = {
                    'agentId': 'DRSOAFDOTR',         # Reemplazá con tu agente real si hace falta
                    'agentAliasId': 'XKJTFFEMPC',    # Reemplazá si tenés otro alias
                    'sessionId': session_id,
                    'inputText': input_text,
                    'enableTrace': False,
                }

                response = client.invoke_agent(**params)

                return response

            else:
                return None
            

def is_context_independent_heuristic(question: str) -> bool | None:
        """
        Devuelve:
        True  -> seguro independiente
        False -> seguro dependiente
        None  -> dudoso, hay que consultar al LLM
        """
        q = question.strip().lower()

        if len(q.split()) < 3:
            return False

        for phrase in CONTEXT_DEPENDENT_PHRASES:
            if phrase in q:
                return False

        if q.endswith("?") and len(q) < 10:
            return False

        if q.startswith(("divididas", "separadas", "por ")):
            return False

        verbos_comunes = {"son", "fueron", "hubo", "hay", "serán", "tiene", "mostrar", "listar"}
        if any(v in q for v in verbos_comunes):
            return True

        return None

def classify_with_bedrock(question: str) -> bool:
        """
        Usa un LLM en Bedrock para decidir.
        Devuelve True si es independiente, False si es dependiente.
        """
        client = boto3.client("bedrock-runtime", region_name="us-east-1")

        prompt = f"""Human:
        Classify if this question needs previous conversation context.

        Question: "{question}"

        Answer only YES or NO:
        - YES = needs previous context (don't cache)
        - NO = standalone question (cache it)

        Assistant:
        """

        response = client.invoke_model(
        modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 10,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ]
        }))


        result = json.loads(response["body"].read())
        output = result["content"][0]["text"]

        return output == "NO"

def get_agent_id(user_email:str):
    dict_data = json.loads(os.environ["AGENT_TO_USERS"])
    for key, value in dict_data.items():
        if user_email in value:
            return key

    return "XKJTFFEMPC"