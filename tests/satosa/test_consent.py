import json
import re
from collections import Counter
from unittest.mock import Mock
from urllib.parse import urlparse, parse_qs

import pytest
import requests
import responses
from jwkest.jwk import RSAKey, rsa_load
from jwkest.jws import JWS

from satosa.consent import ConsentModule, UnexpectedResponseError
from satosa.context import Context
from satosa.internal_data import InternalResponse, UserIdHashType, InternalRequest, \
    AuthenticationInformation
from satosa.response import Redirect
from satosa.satosa_config import SATOSAConfig

FILTER = ["displayName", "co"]
CONSENT_SERVICE_URL = "https://consent.example.com"
ATTRIBUTES = {"displayName": ["Test"], "co": ["example"], "sn": ["should be removed by consent filter"]}
USER_ID_ATTR = "user_id"


class TestConsent:
    @pytest.fixture
    def satosa_config(self, signing_key_path):
        consent_config = {
            "api_url": CONSENT_SERVICE_URL,
            "redirect_url": "{}/consent".format(CONSENT_SERVICE_URL),
            "sign_key": signing_key_path,
            "state_enc_key": "fsghajf90984jkflds",
        }
        satosa_config = {
            "BASE": "https://proxy.example.com",
            "USER_ID_HASH_SALT": "qwerty",
            "COOKIE_STATE_NAME": "SATOSA_SATE",
            "STATE_ENCRYPTION_KEY": "ASDasd123",
            "BACKEND_MODULES": "",
            "FRONTEND_MODULES": "",
            "INTERNAL_ATTRIBUTES": {"attributes": {}, "user_id_to_attr": USER_ID_ATTR},
            "CONSENT": consent_config
        }

        return SATOSAConfig(satosa_config)

    @pytest.fixture(autouse=True)
    def create_module(self, satosa_config):
        mock_callback = Mock(side_effect=lambda context, internal_resp: (context, internal_resp))
        self.consent_module = ConsentModule(satosa_config, mock_callback)

    @pytest.fixture
    def internal_response(self):
        auth_info = AuthenticationInformation("auth_class_ref", "timestamp", "issuer")
        internal_response = InternalResponse(auth_info=auth_info)
        internal_response.requester = "client"
        internal_response.attributes = ATTRIBUTES
        return internal_response

    @pytest.fixture
    def internal_request(self):
        req = InternalRequest(UserIdHashType.persistent, "example_requester")
        req.add_filter(FILTER + ["sn"])
        return req

    @pytest.fixture(scope="session")
    def consent_verify_endpoint_regex(self):
        return re.compile(r"{}/verify/.*".format(CONSENT_SERVICE_URL))

    @pytest.fixture(scope="session")
    def consent_registration_endpoint_regex(self):
        return re.compile(r"{}/creq/.*".format(CONSENT_SERVICE_URL))

    def assert_redirect(self, redirect_resp, expected_ticket):
        assert isinstance(redirect_resp, Redirect)

        parsed_url = parse_qs(urlparse(redirect_resp.message).query)
        assert len(parsed_url["ticket"]) == 1
        ticket = parsed_url["ticket"][0]
        assert ticket == expected_ticket

    def assert_registration_req(self, request, internal_response, satosa_config):
        split_path = request.path_url.lstrip("/").split("/")
        assert len(split_path) == 2
        jwks = split_path[1]

        # Verify signature
        sign_key = RSAKey(key=rsa_load(satosa_config["CONSENT"]["sign_key"]), use="sig")
        jws = JWS()
        jws.verify_compact(jwks, [sign_key])

        consent_args = jws.msg
        assert consent_args["attr"] == internal_response.attributes
        assert consent_args["redirect_endpoint"] == satosa_config["BASE"] + "/consent/handle_consent"
        assert consent_args["requester_name"] == internal_response.requester
        assert consent_args["locked_attrs"] == [USER_ID_ATTR]
        assert "id" in consent_args

    def test_disabled_consent(self, satosa_config):
        mock_callback = Mock()
        satosa_config["CONSENT"]["enable"] = False

        consent_module = ConsentModule(satosa_config, mock_callback)
        assert consent_module.enabled == False
        assert not hasattr(consent_module, 'proxy_base')

        consent_module.manage_consent(None, None)
        assert mock_callback.called

    @responses.activate
    def test_verify_consent_false_on_http_400(self, satosa_config):
        consent_id = "1234"
        responses.add(responses.GET,
                      "{}/verify/{}".format(satosa_config["CONSENT"]["api_url"], consent_id),
                      status=400)
        assert not self.consent_module._verify_consent(consent_id)

    @responses.activate
    def test_verify_consent(self, satosa_config):
        consent_id = "1234"
        responses.add(responses.GET,
                      "{}/verify/{}".format(satosa_config["CONSENT"]["api_url"], consent_id),
                      status=200, body=json.dumps(FILTER))
        assert self.consent_module._verify_consent(consent_id) == FILTER

    @pytest.mark.parametrize('status', [
        401, 404, 418, 500
    ])
    @responses.activate
    def test_consent_registration_raises_on_unexpected_status_code(self, status, satosa_config):
        responses.add(responses.GET, re.compile(r"{}/creq/.*".format(satosa_config["CONSENT"]["api_url"])),
                      status=status)
        with pytest.raises(UnexpectedResponseError):
            self.consent_module._consent_registration({})

    @responses.activate
    def test_consent_registration(self, satosa_config):
        responses.add(responses.GET, re.compile(r"{}/creq/.*".format(satosa_config["CONSENT"]["api_url"])),
                      status=200, body="ticket")
        assert self.consent_module._consent_registration({}) == "ticket"

    @responses.activate
    def test_consent_handles_connection_error(self, context, internal_response, internal_request,
                                              consent_verify_endpoint_regex):
        responses.add(responses.GET,
                      consent_verify_endpoint_regex,
                      body=requests.ConnectionError("No connection"))
        self.consent_module.save_state(internal_request, context.state)
        with responses.RequestsMock(assert_all_requests_are_fired=True) as rsps:
            rsps.add(responses.GET,
                     consent_verify_endpoint_regex,
                     body=requests.ConnectionError("No connection"))
            context, internal_response = self.consent_module.manage_consent(context, internal_response)

        assert context
        assert not internal_response.attributes

    @responses.activate
    def test_consent_prev_given(self, context, internal_response, internal_request,
                                consent_verify_endpoint_regex):
        responses.add(responses.GET, consent_verify_endpoint_regex, status=200,
                      body=json.dumps(FILTER))

        self.consent_module.save_state(internal_request, context.state)
        context, internal_response = self.consent_module.manage_consent(context, internal_response)
        assert context
        assert "displayName" in internal_response.attributes

    def test_consent_full_flow(self, context, satosa_config, internal_response, internal_request,
                               consent_verify_endpoint_regex, consent_registration_endpoint_regex):
        expected_ticket = "my_ticket"

        self.consent_module.save_state(internal_request, context.state)

        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, consent_verify_endpoint_regex, status=401)
            rsps.add(responses.GET, consent_registration_endpoint_regex, status=200,
                     body=expected_ticket)
            resp = self.consent_module.manage_consent(context, internal_response)

            self.assert_redirect(resp, expected_ticket)
            self.assert_registration_req(rsps.calls[1].request,
                                         internal_response,
                                         satosa_config)

        with responses.RequestsMock() as rsps:
            # Now consent has been given, consent service returns 200 OK
            rsps.add(responses.GET, consent_verify_endpoint_regex, status=200,
                     body=json.dumps(FILTER))

            context, internal_response = self.consent_module._handle_consent_response(context)

        assert internal_response.attributes["displayName"] == ["Test"]
        assert internal_response.attributes["co"] == ["example"]
        assert "sn" not in internal_response.attributes  # 'sn' should be filtered

    @responses.activate
    def test_consent_not_given(self, context, satosa_config, internal_response, internal_request,
                               consent_verify_endpoint_regex, consent_registration_endpoint_regex):
        expected_ticket = "my_ticket"

        responses.add(responses.GET, consent_verify_endpoint_regex, status=401)
        responses.add(responses.GET, consent_registration_endpoint_regex, status=200,
                      body=expected_ticket)

        self.consent_module.save_state(internal_request, context.state)

        resp = self.consent_module.manage_consent(context, internal_response)

        self.assert_redirect(resp, expected_ticket)
        self.assert_registration_req(responses.calls[1].request,
                                     internal_response,
                                     satosa_config)

        new_context = Context()
        new_context.state = context.state
        # Verify endpoint of consent service still gives 401 (no consent given)
        context, internal_response = self.consent_module._handle_consent_response(context)
        assert not internal_response.attributes

    def test_get_consent_id(self):
        attributes = {"foo": ["bar", "123"], "abc": ["xyz", "456"]}

        id = self.consent_module._get_consent_id("test-requester", "user1", attributes)
        assert id == "ZTRhMTJmNWQ2Yjk2YWE0YzgyMzU4NTlmNjM3YjlhNmQ4ZjZiODMzOTQ0ZjNiMTVmODEwMDhmMDg5N2JlMDg0Y2ZkZGFkOTkzMDZiNDZiNjMxNzBkYzExOTcxN2RkMzJjMmY5NzRhZDA2NjYxMTg0NjkyYzdjN2IxNTRiZDkwNmM="

    def test_filter_attributes(self):
        filtered_attributes = self.consent_module._filter_attributes(ATTRIBUTES, FILTER)
        assert Counter(filtered_attributes.keys()) == Counter(FILTER)
