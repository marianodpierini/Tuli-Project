import json
import logging
import os
import time
from typing import Any, Dict, Iterable, Optional
from urllib.request import urlopen

import jwt
from jwt import InvalidTokenError
from jwt.algorithms import RSAAlgorithm


logger = logging.getLogger()
logger.setLevel(logging.INFO)


ISSUER = os.environ["JWT_ISSUER"]
AUDIENCE = os.environ["JWT_AUDIENCE"]
JWKS_URL = os.environ["JWKS_URL"]

# Optional hardening knobs.
JWT_LEEWAY_SECONDS = int(os.getenv("JWT_LEEWAY_SECONDS", "0"))
REQUIRED_SCOPES = {s.strip() for s in os.getenv("REQUIRED_SCOPES", "").split(",") if s.strip()}
REQUIRED_ROLES = {r.strip() for r in os.getenv("REQUIRED_ROLES", "").split(",") if r.strip()}

# JWK cache in memory (per warm Lambda runtime).
JWKS_CACHE_TTL_SECONDS = int(os.getenv("JWKS_CACHE_TTL_SECONDS", "300"))
_jwks_cache: Dict[str, Any] = {"expires_at": 0, "keys_by_kid": {}}

# Keep context compact and explicit.
CONTEXT_CLAIMS = [
	c.strip()
	for c in os.getenv(
		"CONTEXT_CLAIMS",
		"sub,client_id,scope,roles,tenant,iss,aud",
	).split(",")
	if c.strip()
]


def _to_set(value: Any) -> set:
	if value is None:
		return set()
	if isinstance(value, str):
		# OAuth2 scopes are usually space-delimited.
		return {part for part in value.replace(",", " ").split() if part}
	if isinstance(value, Iterable):
		return {str(v) for v in value}
	return {str(value)}


def _get_bearer_token(event: Dict[str, Any]) -> str:
	# TOKEN authorizer (REST API): authorizationToken + methodArn.
	authorization = event.get("authorizationToken")

	# REQUEST authorizer (REST/HTTP API): headers.Authorization.
	if not authorization:
		headers = event.get("headers") or {}
		authorization = headers.get("Authorization") or headers.get("authorization")

	# Hard reject token on query string.
	query_params = event.get("queryStringParameters") or {}
	if any(key in query_params for key in ("token", "access_token", "jwt")):
		raise PermissionError("Token in query string is not allowed")

	if not authorization:
		raise PermissionError("Missing Authorization header")

	parts = authorization.split()
	if len(parts) != 2 or parts[0].lower() != "bearer":
		raise PermissionError("Authorization must use Bearer token")

	return parts[1]


def _fetch_jwks() -> Dict[str, Any]:
	now = int(time.time())
	if _jwks_cache["keys_by_kid"] and now < _jwks_cache["expires_at"]:
		return _jwks_cache["keys_by_kid"]

	with urlopen(JWKS_URL, timeout=5) as response:
		jwks = json.loads(response.read().decode("utf-8"))

	keys_by_kid = {}
	for jwk in jwks.get("keys", []):
		kid = jwk.get("kid")
		if not kid:
			continue
		keys_by_kid[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))

	if not keys_by_kid:
		raise RuntimeError("JWKS does not contain valid RSA keys")

	_jwks_cache["keys_by_kid"] = keys_by_kid
	_jwks_cache["expires_at"] = now + JWKS_CACHE_TTL_SECONDS
	return keys_by_kid


def _verify_token(token: str) -> Dict[str, Any]:
	unverified_header = jwt.get_unverified_header(token)
	alg = unverified_header.get("alg")
	kid = unverified_header.get("kid")

	if alg != "RS256":
		raise PermissionError("Unsupported token algorithm")
	if not kid:
		raise PermissionError("Missing key id in token header")

	keys_by_kid = _fetch_jwks()
	public_key = keys_by_kid.get(kid)

	# Key rotation safety: refresh once if kid is unknown.
	if public_key is None:
		_jwks_cache["expires_at"] = 0
		keys_by_kid = _fetch_jwks()
		public_key = keys_by_kid.get(kid)

	if public_key is None:
		raise PermissionError("Unknown key id")

	try:
		claims = jwt.decode(
			token,
			key=public_key,
			algorithms=["RS256"],
			audience=AUDIENCE,
			issuer=ISSUER,
			options={"require": ["exp", "iss", "aud"]},
			leeway=JWT_LEEWAY_SECONDS,
		)
	except InvalidTokenError as exc:
		raise PermissionError("Token validation failed") from exc

	token_scopes = _to_set(claims.get("scope"))
	token_roles = _to_set(claims.get("roles"))

	if REQUIRED_SCOPES and not REQUIRED_SCOPES.issubset(token_scopes):
		raise PermissionError("Missing required scope")

	if REQUIRED_ROLES and not REQUIRED_ROLES.intersection(token_roles):
		raise PermissionError("Missing required role")

	return claims


def _build_context(claims: Dict[str, Any]) -> Dict[str, Any]:
	context: Dict[str, Any] = {}
	for claim in CONTEXT_CLAIMS:
		if claim not in claims:
			continue
		value = claims[claim]
		if isinstance(value, (dict, list, tuple, set)):
			context[claim] = json.dumps(value)
		elif isinstance(value, (str, int, float, bool)):
			context[claim] = value
		else:
			context[claim] = str(value)
	return context


def _policy(principal_id: str, effect: str, resource_arn: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
	response = {
		"principalId": principal_id,
		"policyDocument": {
			"Version": "2012-10-17",
			"Statement": [
				{
					"Action": "execute-api:Invoke",
					"Effect": effect,
					"Resource": resource_arn,
				}
			],
		},
	}

	if context:
		response["context"] = context

	return response


def _is_http_api_v2_event(event: Dict[str, Any]) -> bool:
	return str(event.get("version", "")).startswith("2")


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
	logger.info("Received event keys: %s", list(event.keys()))

	resource_arn = event.get("methodArn") or event.get("routeArn") or "*"
	is_http_v2 = _is_http_api_v2_event(event)

	try:
		token = _get_bearer_token(event)
		claims = _verify_token(token)
		principal_id = str(claims.get("sub") or claims.get("client_id") or "service")
		context_map = _build_context(claims)
	except PermissionError as exc:
		logger.warning("Authorization failed: %s", exc)
		if is_http_v2:
			# HTTP API simple response mode.
			return {"isAuthorized": False}
		return _policy("unauthorized", "Deny", resource_arn)
	except Exception:
		logger.exception("Unexpected authorizer error")
		if is_http_v2:
			return {"isAuthorized": False}
		return _policy("unauthorized", "Deny", resource_arn)

	if is_http_v2:
		# HTTP API simple response mode.
		return {
			"isAuthorized": True,
			"context": context_map,
		}

	# REST API / TOKEN authorizer response.
	return _policy(principal_id, "Allow", resource_arn, context_map)
