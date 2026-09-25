"""D1: speaker → profile routing (pure module, no orchestrator imports)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_agent_routing import (RoutingError, health_url, missing_tokens, parse_speaker_profiles,  # noqa: E402
                              select_target, token_env_name)

VOICE = {"AtomS3R", "NabuVoice"}
MAP = parse_speaker_profiles('{"Marco": "hermes-marco", "Ada": "hermes-ada"}')
ENV = {"AI_AGENT_TOKEN_HERMES_MARCO": "t-marco", "AI_AGENT_TOKEN_HERMES_ADA": "t-ada",
       "AI_AGENT_TOKEN_HERMES_SHARED": "t-shared"}


def sel(ctx, mode="multiplex", env=ENV, **kw):
    args = dict(mode=mode, legacy_url="http://h:3020", legacy_voice_url="http://h:8642", legacy_token="t-legacy",
                voice_sources=VOICE, mux_url="http://h:8642/", speaker_profiles=MAP,
                default_profile="hermes-shared", env=env)
    args.update(kw)
    return select_target(ctx, **args)


class Multiplex(unittest.TestCase):
    def test_identified_speaker_goes_to_own_profile_with_own_token(self):
        t = sel({"speaker_name": "Marco", "speaker_identified": True, "source": "AtomS3R"})
        self.assertEqual((t.base_url, t.token, t.profile), ("http://h:8642/p/hermes-marco", "t-marco", "hermes-marco"))
        t = sel({"speaker_name": " ada ", "speaker_identified": True, "source": "web"})
        self.assertEqual((t.base_url, t.token), ("http://h:8642/p/hermes-ada", "t-ada"))

    def test_unidentified_never_reaches_a_private_profile(self):
        for ctx in ({"speaker_name": "Marco", "speaker_identified": False},
                    {"speaker_name": "Marco"},
                    {"speaker_name": "Marco", "speaker_identified": "true"},   # only a real True counts
                    {"speaker_name": "Sconosciuto", "speaker_identified": True},
                    {}):
            self.assertEqual(sel(ctx).profile, "hermes-shared", ctx)

    def test_voice_and_text_lanes_route_the_same_way(self):
        a = sel({"speaker_name": "Ada", "speaker_identified": True, "source": "AtomS3R"})
        b = sel({"speaker_name": "Ada", "speaker_identified": True, "source": "web"})
        self.assertEqual(a, b)

    def test_missing_credential_is_an_error_not_a_fallback(self):
        env = {k: v for k, v in ENV.items() if k != "AI_AGENT_TOKEN_HERMES_ADA"}
        with self.assertRaises(RoutingError) as e:
            sel({"speaker_name": "Ada", "speaker_identified": True}, env=env)
        self.assertIn("AI_AGENT_TOKEN_HERMES_ADA", str(e.exception))
        self.assertNotIn("t-", str(e.exception))

    def test_no_mux_url_or_bad_mode_fails(self):
        with self.assertRaises(RoutingError):
            sel({}, mux_url="")
        with self.assertRaises(RoutingError):
            sel({}, mode="Multiplexx")

    def test_url_is_a_base_without_v1(self):
        self.assertFalse(sel({}).base_url.endswith("/v1"))


class Legacy(unittest.TestCase):
    def test_legacy_is_unchanged(self):
        t = sel({"speaker_name": "Marco", "speaker_identified": True, "source": "AtomS3R"}, mode="legacy")
        self.assertEqual((t.base_url, t.token, t.profile), ("http://h:8642", "t-legacy", None))
        t = sel({"source": "web"}, mode="legacy")
        self.assertEqual(t.base_url, "http://h:3020")
        t = sel({"source": "AtomS3R"}, mode="legacy", legacy_voice_url="")
        self.assertEqual(t.base_url, "http://h:3020")


class Config(unittest.TestCase):
    def test_speaker_map_is_validated(self):
        for bad in ('["hermes-marco"]', '{"Marco": "default"}', '{"Marco": "../x"}', '{"Marco": 1}'):
            with self.assertRaises(ValueError, msg=bad):
                parse_speaker_profiles(bad)

    def test_token_names_and_missing_tokens(self):
        self.assertEqual(token_env_name("hermes-marco"), "AI_AGENT_TOKEN_HERMES_MARCO")
        self.assertEqual(missing_tokens(MAP, "hermes-shared", {"AI_AGENT_TOKEN_HERMES_MARCO": "x"}),
                         ["AI_AGENT_TOKEN_HERMES_ADA", "AI_AGENT_TOKEN_HERMES_SHARED"])

    def test_health_url_follows_the_mode(self):
        self.assertEqual(health_url("multiplex", "http://h:3020", "http://h:8642/"), "http://h:8642/health")
        self.assertEqual(health_url("legacy", "http://h:3020/", "http://h:8642"), "http://h:3020/health")


if __name__ == "__main__":
    unittest.main()
