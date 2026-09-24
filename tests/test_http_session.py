from src.http_session import make_session


class TestMakeSession:
    def test_sets_the_given_user_agent(self):
        session = make_session("KukistiBot-Test/1.0")
        assert session.headers["User-Agent"] == "KukistiBot-Test/1.0"

    def test_defaults_to_accepting_json(self):
        session = make_session("KukistiBot-Test/1.0")
        assert session.headers["Accept"] == "application/json"

    def test_accept_json_false_leaves_the_default_accept_header(self):
        session = make_session("KukistiBot-Test/1.0", accept_json=False)
        assert session.headers.get("Accept") == "*/*"
