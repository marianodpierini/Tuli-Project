import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set, Tuple
from urllib import error, parse, request


logger = logging.getLogger()
logger.setLevel(logging.INFO)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_WORKSPACE_DOMAIN = os.getenv("GOOGLE_WORKSPACE_DOMAIN", "aero.tur.ar").strip().lower()
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
ALLOWED_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


def _env_csv(name: str) -> Set[str]:
	raw = os.getenv(name, "")
	return {item.strip().lower() for item in raw.split(",") if item.strip()}


ALLOWED_EMAILS = _env_csv("GOOGLE_ALLOWED_EMAILS")


class AuthError(Exception):
	def __init__(self, reason: str, status_code: int):
		super().__init__(reason)
		self.reason = reason
		self.status_code = status_code


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

	parts = str(auth_value).strip().split()
	if len(parts) == 2 and parts[0].lower() == "bearer":
		return parts[1].strip()

	raise AuthError("invalid_authorization_header", 401)


def _fetch_google_token_info(id_token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
	url = f"{GOOGLE_TOKENINFO_URL}?{parse.urlencode({'id_token': id_token})}"
	req = request.Request(url, method="GET")

	try:
		with request.urlopen(req, timeout=5) as resp:
			body = resp.read().decode("utf-8")
			return json.loads(body), None
	except error.HTTPError as exc:
		raw = exc.read().decode("utf-8", errors="ignore")
		if exc.code in (400, 401):
			return None, "invalid_or_expired_google_token"
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

	email = (claims.get("email") or "").strip().lower()
	if not email:
		return "missing_email_claim"

	email_verified = claims.get("email_verified")
	if str(email_verified).strip().lower() != "true":
		return "email_not_verified"

	if not ALLOWED_EMAILS:
		return "missing_allowed_emails_env"

	if email not in ALLOWED_EMAILS:
		return "email_not_whitelisted"

	try:
		exp = int(claims.get("exp", "0"))
	except (TypeError, ValueError):
		return "invalid_exp"

	if exp <= int(time.time()):
		return "token_expired"

	return None


def lambda_handler(event, context):  # noqa: ARG001
	try:
		token = _extract_bearer_token(event or {})
		if not token:
			raise AuthError("missing_bearer_token", 401)

		claims, err = _fetch_google_token_info(token)
		if err:
			raise AuthError(err, 401)

		validation_error = _validate_google_claims(claims or {})
		if validation_error == "email_not_whitelisted":
			raise AuthError(validation_error, 403)
		if validation_error:
			raise AuthError(validation_error, 401)

		principal = (claims.get("email") or claims.get("sub") or "google-user").strip()
		auth_context = {
			"email": str(claims.get("email", "")),
			"sub": str(claims.get("sub", "")),
			"hd": str(claims.get("hd", "")),
			"aud": str(claims.get("aud", "")),
			"email_verified": str(claims.get("email_verified", "")),
		}

		return _build_auth_response(
			event=event or {},
			is_authorized=True,
			principal_id=principal,
			context=auth_context,
		)
	except AuthError as exc:
		if exc.status_code == 403:
			return _deny_response(event or {}, exc.reason)

		# For REST API custom authorizers, raising "Unauthorized" results in HTTP 401.
		raise Exception("Unauthorized")
