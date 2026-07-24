import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib import error, parse, request


logger = logging.getLogger()
logger.setLevel(logging.INFO)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_WORKSPACE_DOMAIN = os.getenv("GOOGLE_WORKSPACE_DOMAIN", "aero.tur.ar").strip().lower()
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
ALLOWED_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


def _deny_response(event: Dict[str, Any], reason: str) -> Dict[str, Any]:
	logger.warning("Authorization denied: %s", reason)
	return _build_auth_response(event, is_authorized=False, principal_id="anonymous", context={"reason": reason})


def _build_auth_response(
	event: Dict[str, Any],
	is_authorized: bool,
	principal_id: str,
	context: Dict[str, str],
) -> Dict[str, Any]:
	method_arn = event.get("methodArn", "*")

	# HTTP API Lambda authorizer v2 simple response.
	if event.get("version") == "2.0":
		return {
			"isAuthorized": is_authorized,
			"context": context,
		}

	effect = "Allow" if is_authorized else "Deny"
	return {
		"principalId": principal_id,
		"policyDocument": {
			"Version": "2012-10-17",
			"Statement": [
				{
					"Action": "execute-api:Invoke",
					"Effect": effect,
					"Resource": method_arn,
				}
			],
		},
		"context": context,
	}


def _extract_bearer_token(event: Dict[str, Any]) -> Optional[str]:
	auth_value = event.get("authorizationToken")

	if not auth_value:
		headers = event.get("headers") or {}
		auth_value = headers.get("Authorization") or headers.get("authorization")

	if not auth_value:
		return None

	auth_value = auth_value.strip()
	if auth_value.lower().startswith("bearer "):
		return auth_value[7:].strip()

	return auth_value


def _fetch_google_token_info(id_token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
	url = f"{GOOGLE_TOKENINFO_URL}?{parse.urlencode({'id_token': id_token})}"
	req = request.Request(url, method="GET")

	try:
		with request.urlopen(req, timeout=5) as resp:
			body = resp.read().decode("utf-8")
			return json.loads(body), None
	except error.HTTPError as exc:
		raw = exc.read().decode("utf-8", errors="ignore")
		return None, f"google_http_error_{exc.code}:{raw}"
	except Exception as exc:  # noqa: BLE001
		return None, f"google_request_error:{exc}"


def _validate_google_claims(claims: Dict[str, Any]) -> Optional[str]:
	if not GOOGLE_CLIENT_ID:
		return "missing_google_client_id_env"

	aud = (claims.get("aud") or "").strip()
	if aud != GOOGLE_CLIENT_ID:
		return "invalid_audience"

	issuer = (claims.get("iss") or "").strip()
	if issuer not in ALLOWED_ISSUERS:
		return "invalid_issuer"

	hosted_domain = (claims.get("hd") or "").strip().lower()
	if hosted_domain != GOOGLE_WORKSPACE_DOMAIN:
		return "invalid_hosted_domain"

	try:
		exp = int(claims.get("exp", "0"))
	except (TypeError, ValueError):
		return "invalid_exp"

	if exp <= int(time.time()):
		return "token_expired"

	return None


def lambda_handler(event, context):  # noqa: ARG001
	token = _extract_bearer_token(event or {})
	if not token:
		return _deny_response(event or {}, "missing_bearer_token")

	claims, err = _fetch_google_token_info(token)
	if err:
		return _deny_response(event or {}, err)

	validation_error = _validate_google_claims(claims or {})
	if validation_error:
		return _deny_response(event or {}, validation_error)

	principal = (claims.get("email") or claims.get("sub") or "google-user").strip()
	auth_context = {
		"email": str(claims.get("email", "")),
		"sub": str(claims.get("sub", "")),
		"hd": str(claims.get("hd", "")),
		"aud": str(claims.get("aud", "")),
	}

	return _build_auth_response(
		event=event or {},
		is_authorized=True,
		principal_id=principal,
		context=auth_context,
	)
