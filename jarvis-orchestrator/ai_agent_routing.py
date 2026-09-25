"""Speaker → Hermes profile routing towards the multiplex gateway (D1).

legacy    unchanged: AI_AGENT_URL (voice sources: AI_AGENT_URL_VOICE) + AI_AGENT_TOKEN;
          the Hermes side picks the profile (wa-router :3020, speaker-routing on shared).
multiplex the Orchestrator picks it: an IDENTIFIED speaker listed in
          AI_AGENT_SPEAKER_PROFILES goes to that profile, anyone else (unidentified,
          unknown) to AI_AGENT_DEFAULT_PROFILE — never a private profile. URL and token
          are chosen together: <AI_AGENT_MUX_URL>/p/<profile> (callers append /v1/...)
          with AI_AGENT_TOKEN_<PROFILE>. A missing credential is an explicit error, never
          a fallback to another profile or to legacy.

AI_AGENT_TOKEN is NOT used in multiplex for outbound calls: it stays the inbound token
of tools_api.py and the exec-approval WebSocket.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping, Optional

MODES = ("legacy", "multiplex")
_PROFILE_RE = re.compile(r"^hermes-[a-z0-9][a-z0-9-]*$")


class RoutingError(RuntimeError):
    """The request cannot be routed; the caller must fail explicitly."""


@dataclass(frozen=True)
class AgentTarget:
    base_url: str               # no trailing /v1
    token: str
    profile: Optional[str]      # None in legacy mode


def token_env_name(profile: str) -> str:
    return "AI_AGENT_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", profile.upper())


def parse_speaker_profiles(raw: str) -> dict:
    """'{"Marco": "hermes-marco"}' → {"marco": "hermes-marco"}; rejects malformed maps."""
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("AI_AGENT_SPEAKER_PROFILES must be a JSON object")
    out = {}
    for name, profile in data.items():
        if not isinstance(profile, str) or not _PROFILE_RE.match(profile):
            raise ValueError(f"invalid profile for speaker {name!r}: {profile!r}")
        out[str(name).strip().lower()] = profile
    return out


def select_target(context: Mapping, *, mode: str, legacy_url: str, legacy_voice_url: str,
                  legacy_token: str, voice_sources, mux_url: str, speaker_profiles: Mapping[str, str],
                  default_profile: str, env: Mapping[str, str]) -> AgentTarget:
    if mode == "legacy":
        url = legacy_url
        if legacy_voice_url and context.get("source") in voice_sources:
            url = legacy_voice_url
        if not url:
            raise RoutingError("AI_AGENT_URL not set")
        return AgentTarget(url.rstrip("/"), legacy_token, None)
    if mode != "multiplex":
        raise RoutingError(f"unknown AI_AGENT_ROUTING_MODE {mode!r} (expected one of {MODES})")
    if not mux_url:
        raise RoutingError("AI_AGENT_MUX_URL not set")
    if not _PROFILE_RE.match(default_profile or ""):
        raise RoutingError(f"invalid AI_AGENT_DEFAULT_PROFILE {default_profile!r}")
    name = str(context.get("speaker_name") or "").strip().lower()
    profile = None
    if context.get("speaker_identified") is True and name:
        profile = speaker_profiles.get(name)
    profile = profile or default_profile
    token = env.get(token_env_name(profile), "")
    if not token:
        raise RoutingError(f"no credential for profile {profile} ({token_env_name(profile)} not set)")
    return AgentTarget(f"{mux_url.rstrip('/')}/p/{profile}", token, profile)


def health_url(mode: str, legacy_url: str, mux_url: str) -> str:
    base = mux_url if mode == "multiplex" else legacy_url
    return f"{(base or '').rstrip('/')}/health"


def missing_tokens(speaker_profiles: Mapping[str, str], default_profile: str, env: Mapping[str, str]) -> list:
    """Names (never values) of the per-profile tokens multiplex mode needs but lacks."""
    names = {token_env_name(p) for p in set(speaker_profiles.values()) | {default_profile}}
    return sorted(n for n in names if not env.get(n))
