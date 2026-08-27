import json
import importlib
import logging
import os
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.request import urlopen


logger = logging.getLogger()
logger.setLevel(logging.INFO)


_JWKS_CACHE: Dict[str, object] = {
	"jwks": None,
	"expires_at": 0,
}


def _load_jwt_module():
	try:
		return importlib.import_module("jwt")
	except Exception as exc:
		raise RuntimeError(
			"Missing dependency 'PyJWT'. Add it to the Lambda package (pip install PyJWT cryptography)."
		) from exc


def _env_csv(name: str) -> List[str]:
	raw = os.getenv(name, "")
	return [item.strip() for item in raw.split(",") if item.strip()]


def _issuer() -> str:
	configured = os.getenv("COGNITO_ISSUER", "").strip()
	if configured:
		return configured.rstrip("/")

	region = os.getenv("COGNITO_REGION", "").strip()
	user_pool_id = os.getenv("COGNITO_USER_POOL_ID", "").strip()
	if not region or not user_pool_id:
		raise ValueError(
			"Missing Cognito config. Set COGNITO_ISSUER or both COGNITO_REGION and COGNITO_USER_POOL_ID"
		)

	return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def _jwks_url(issuer: str) -> str:
	return f"{issuer}/.well-known/jwks.json"


def _get_jwks(issuer: str) -> dict:
	now = int(time.time())
	if _JWKS_CACHE["jwks"] and now < int(_JWKS_CACHE["expires_at"]):
		return _JWKS_CACHE["jwks"]

	cache_ttl = int(os.getenv("JWKS_CACHE_TTL_SECONDS", "300"))
	with urlopen(_jwks_url(issuer), timeout=5) as response:
		jwks = json.loads(response.read().decode("utf-8"))

	_JWKS_CACHE["jwks"] = jwks
	_JWKS_CACHE["expires_at"] = now + max(30, cache_ttl)
	return jwks


def _extract_bearer_token(event: dict) -> str:
	token_source = event.get("authorizationToken")

	if not token_source:
		headers = event.get("headers") or {}
		token_source = headers.get("Authorization") or headers.get("authorization")

	if not token_source:
		raise Exception("Unauthorized")

	parts = token_source.split()
	if len(parts) != 2 or parts[0].lower() != "bearer":
		raise Exception("Unauthorized")

	return parts[1].strip()


def _parse_method_and_path_from_arn(method_arn: str) -> Tuple[str, str]:
	# arn:aws:execute-api:{region}:{account}:{apiId}/{stage}/{method}/{resourcePath}
	try:
		arn_parts = method_arn.split(":", 5)
		api_gateway_part = arn_parts[5]
		path_parts = api_gateway_part.split("/")
		method = path_parts[2] if len(path_parts) > 2 else "*"
		resource_path = "/" + "/".join(path_parts[3:]) if len(path_parts) > 3 else "/"
		return method.upper(), resource_path
	except Exception:
		return "*", "/"


def _load_scopes_config() -> Tuple[Set[str], Dict[str, Set[str]]]:
	required_scopes_global = set(_env_csv("REQUIRED_SCOPES"))

	required_scopes_by_route: Dict[str, Set[str]] = {}
	mapping_raw = os.getenv("REQUIRED_SCOPES_BY_ROUTE", "").strip()
	if mapping_raw:
		parsed = json.loads(mapping_raw)
		if not isinstance(parsed, dict):
			raise ValueError("REQUIRED_SCOPES_BY_ROUTE must be a JSON object")

		for route_key, scopes in parsed.items():
			key = str(route_key).strip().upper()
			if isinstance(scopes, str):
				required_scopes_by_route[key] = {
					scope.strip() for scope in scopes.split() if scope.strip()
				}
			elif isinstance(scopes, list):
				required_scopes_by_route[key] = {
					str(scope).strip() for scope in scopes if str(scope).strip()
				}
			else:
				raise ValueError(
					"Each REQUIRED_SCOPES_BY_ROUTE value must be string or list"
				)

	return required_scopes_global, required_scopes_by_route


def _token_scopes(claims: dict) -> Set[str]:
	scope_claim = claims.get("scope", "")
	scp_claim = claims.get("scp", [])
	scopes: Set[str] = set()

	if isinstance(scope_claim, str):
		scopes.update([s.strip() for s in scope_claim.split() if s.strip()])
	if isinstance(scp_claim, str):
		scopes.update([s.strip() for s in scp_claim.split() if s.strip()])
	elif isinstance(scp_claim, list):
		scopes.update([str(s).strip() for s in scp_claim if str(s).strip()])

	return scopes


def _validate_audience_or_client(claims: dict) -> None:
	allowed_audiences = set(_env_csv("ALLOWED_AUDIENCES"))
	allowed_client_ids = set(_env_csv("ALLOWED_CLIENT_IDS"))

	if not allowed_audiences and not allowed_client_ids:
		raise ValueError(
			"Configure ALLOWED_AUDIENCES and/or ALLOWED_CLIENT_IDS for validation"
		)

	token_aud = claims.get("aud")
	token_client_id = claims.get("client_id")

	aud_ok = bool(token_aud and str(token_aud) in allowed_audiences)
	client_ok = bool(token_client_id and str(token_client_id) in allowed_client_ids)

	if not (aud_ok or client_ok):
		raise Exception("Unauthorized")


def _validate_scopes(claims: dict, method_arn: str) -> None:
	required_global, required_by_route = _load_scopes_config()
	method, path = _parse_method_and_path_from_arn(method_arn)
	route_key = f"{method} {path}".upper()

	required = required_by_route.get(route_key, required_global)
	if not required:
		return

	token_scopes = _token_scopes(claims)
	if not required.issubset(token_scopes):
		raise Exception("Unauthorized")


def _get_public_key_for_kid(jwks: dict, kid: str):
	jwt_module = _load_jwt_module()
	keys = jwks.get("keys", []) if isinstance(jwks, dict) else []
	for key in keys:
		if key.get("kid") == kid:
			return jwt_module.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
	raise Exception("Unauthorized")


def _allow_policy(principal_id: str, method_arn: str, context: Optional[dict] = None):
	response = {
		"principalId": principal_id,
		"policyDocument": {
			"Version": "2012-10-17",
			"Statement": [
				{
					"Action": "execute-api:Invoke",
					"Effect": "Allow",
					"Resource": method_arn,
				}
			],
		},
	}
	if context:
		response["context"] = {
			k: str(v)[:1000] for k, v in context.items() if v is not None
		}
	return response


def lambda_handler(event, context):
	try:
		jwt_module = _load_jwt_module()
		token = _extract_bearer_token(event)
		method_arn = event.get("methodArn", "*")

		unverified_header = jwt_module.get_unverified_header(token)
		alg = unverified_header.get("alg")
		kid = unverified_header.get("kid")

		if alg != "RS256" or not kid:
			raise Exception("Unauthorized")

		issuer = _issuer()
		jwks = _get_jwks(issuer)
		public_key = _get_public_key_for_kid(jwks, kid)

		claims = jwt_module.decode(
			token,
			public_key,
			algorithms=["RS256"],
			issuer=issuer,
			options={
				"require": ["iss", "exp"],
				"verify_aud": False,
			},
		)

		if claims.get("token_use") != "access":
			raise Exception("Unauthorized")

		_validate_audience_or_client(claims)
		_validate_scopes(claims, method_arn)

		principal_id = (
			claims.get("sub")
			or claims.get("client_id")
			or claims.get("username")
			or "authorized-client"
		)

		return _allow_policy(
			principal_id=principal_id,
			method_arn=method_arn,
			context={
				"scope": claims.get("scope", ""),
				"client_id": claims.get("client_id", ""),
				"iss": claims.get("iss", ""),
				"token_use": claims.get("token_use", ""),
			},
		)

	except Exception as exc:
		logger.warning("Authorization failed: %s", exc)
		# REST API custom authorizer expects this exact error for 401 responses.
		raise Exception("Unauthorized")
