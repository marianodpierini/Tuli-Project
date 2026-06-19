from typing import List
import os
import json
import boto3
from core.database.models import SuggestedQuestions
from sqlalchemy import text, literal
from decimal import Decimal

from pgvector.sqlalchemy import Vector

from core.database.db import SessionLocal

import re
from typing import Optional

s3 = boto3.client("s3")
_cached_agents = None

CONTEXT_DEPENDENT_PATTERNS = [
    r"\b(eso|esa|esas|esos|aquello|aquellas|aquellos)\b",
    r"\b(lo anterior|lo mismo|lo de antes)\b",
    r"\b(antes|anterior|recién|previamente)\b",
    r"\b(también|entonces)\b",
    r"^(y|entonces|también)\b",
]

COMMON_VERB_PATTERNS = [
    r"\b(hubo|hay|había|serán|son|fueron|tiene|tienen)\b",
    r"\b(mostrar|listar|detallar|calcular)\b",
    r"\b(cuánt[oa]s?|total|promedio)\b",
]

DOMAIN_KEYWORDS_PATTERN = (
    r"\b(ventas|pasajeros|rentabilidad|ingresos|reservas|margen)\b"
)


def normalize_decimals(obj):
    if isinstance(obj, list):
        return [normalize_decimals(i) for i in obj]
    if isinstance(obj, dict):
        return {k: normalize_decimals(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


def normalize_event(event):
    enriched_event = event
    http_method = event.get("httpMethod", "")
    action_group = event.get("actionGroup", "")

    if http_method == "POST" and action_group == "":
        headers = event.get("headers", {})
        body = event.get("body", {})
        body_json = json.loads(body)
        message_payload = body_json.get("chat", {}).get("messagePayload", {})

        raw = body_json.get("raw", {})

        message = message_payload.get("message", {}).get("text", "")
        space_name = message_payload.get("space", {}).get("name", "")
        thread_name = message_payload.get("thread", {}).get("name", "")
        sender_email = (
            message_payload.get("message", {}).get("sender", {}).get("email", "")
        )
        sender_name = (
            message_payload.get("message", {}).get("sender", {}).get("displayName", "")
        )

        enriched_event = {
            "resource": "/webhooks/google",
            "path": "/webhooks/google",
            "httpMethod": "POST",
            "headers": headers,
            "requestContext": event.get("requestContext", {}),
            "body": json.dumps(
                {
                    "text": message,
                    "name": sender_name,
                    "email": sender_email,
                    "space_name": space_name,
                    "thread_name": thread_name,
                    "raw": raw,
                }
            ),
            "isBase64Encoded": False,
            "source": "google_chat",
        }

    return enriched_event


def valite_existing_response(
    session_id: str, keywords: List[str], user_input: str, boto_config
):

    client = boto3.client(
        "bedrock-agent-runtime", region_name="us-east-1", config=boto_config
    )

    query_embedding = cohere_embed(user_input, keywords, boto_config, "search_query")

    with SessionLocal() as session:

        query_vec = literal(query_embedding, type_=Vector(len(query_embedding)))

        distance = SuggestedQuestions.embedding.cosine_distance(query_vec)
        similarity = (1 - distance).label("similarity")

        stmt = (
            session.query(SuggestedQuestions, similarity)
            .filter(
                SuggestedQuestions.activa.is_(True),
                SuggestedQuestions.embedding.isnot(None),
            )
            .order_by(similarity.desc())
            .limit(3)
        )

        results = stmt.all()

    if len(results) > 0:
        best, similarity = results[0]
        if similarity is not None and similarity >= 0.80:
            if best.sql_query is not None:
                sql_query = best.sql_query
                query_results = session.execute(text(sql_query))

                query_result_dicts = [dict(row._mapping) for row in query_results]
                safe_results = normalize_decimals(query_result_dicts)

                input_text = f"""
                        El usuario preguntó: "{user_input}"

                        La consulta SQL asociada (ID {best.id}) devolvió estos resultados:
                        {json.dumps(safe_results, ensure_ascii=False, indent=2)}

                        Por favor responde al usuario en lenguaje natural, breve y clara,
                        usando los resultados de la consulta.
                        """

                params = {
                    "agentId": "DRSOAFDOTR",  # Reemplazá con tu agente real si hace falta
                    "agentAliasId": "XKJTFFEMPC",  # Reemplazá si tenés otro alias
                    "sessionId": session_id,
                    "inputText": input_text,
                    "enableTrace": False,
                }

                response = client.invoke_agent(**params)

                return response
            else:
                return "Pregunta sin query"

    else:
        return None


def is_context_independent_heuristic(question: str) -> Optional[bool]:
    """
    True  -> seguro independiente (cacheable)
    False -> seguro dependiente (NO cacheable)
    None  -> dudoso, consultar LLM
    """

    q = question.strip().lower()

    if len(q.split()) < 3:
        return False

    for pattern in CONTEXT_DEPENDENT_PATTERNS:
        if re.search(pattern, q):
            return False

    if q.endswith("?") and len(q) < 12:
        return False

    if re.search(DOMAIN_KEYWORDS_PATTERN, q):
        return True

    for pattern in COMMON_VERB_PATTERNS:
        if re.search(pattern, q) and len(q.split()) >= 5:
            return True

    if q.endswith("?") and len(q.split()) >= 8:
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
        modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 30,
                "temperature": 0.0,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": prompt}]}
                ],
            }
        ),
    )

    result = json.loads(response["body"].read())
    input_tokens = result["usage"]["input_tokens"]
    output_tokens = result["usage"]["output_tokens"]
    output = result["content"][0]["text"].strip().lower()

    return output == "NO", input_tokens + output_tokens


def get_agents_mapping():
    global _cached_agents

    if _cached_agents is not None:
        return _cached_agents

    bucket = os.environ["S3_BUCKET"]
    key = os.environ["S3_KEY"]

    response = s3.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8")

    _cached_agents = json.loads(content)
    return _cached_agents


def cohere_embed(text: str, keywords: list, boto_config, input_type: str = "search_query") -> list[float]:
    client = boto3.client(
        "bedrock-runtime", region_name="us-east-1", config=boto_config
    )

    text_for_embedding = " ".join(
        [
            text or "",
            " ".join(keywords or []),
        ]
    )

    response = client.invoke_model(
        modelId="cohere.embed-multilingual-v3",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "texts": [text_for_embedding],
            "input_type": input_type,
            })
    )

    body = json.loads(response["body"].read())
    return body["embedding"]
