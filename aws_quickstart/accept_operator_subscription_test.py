#!/usr/bin/env python3

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


if "boto3" not in sys.modules:
    sys.modules["boto3"] = MagicMock()
if "botocore.config" not in sys.modules:
    sys.modules["botocore"] = MagicMock()
    sys.modules["botocore.config"] = MagicMock()
if "cfnresponse" not in sys.modules:
    cfnresponse = MagicMock()
    cfnresponse.SUCCESS = "SUCCESS"
    cfnresponse.FAILED = "FAILED"
    sys.modules["cfnresponse"] = cfnresponse


from accept_operator_subscription import (
    DATADOG_OPERATOR_PRODUCT_ID,
    SubscriptionError,
    create_and_accept_agreement,
    entitlement_status,
    ensure_subscription,
    find_active_agreement,
    find_free_offer,
    handler,
    requested_terms,
    validate_zero_charge_summary,
    wait_for_entitlement,
)
from marketplace_agreement_compat import apply_marketplace_agreement_compatibility


def paginator_client(**operation_pages):
    client = MagicMock()
    paginators = {}
    for operation, pages in operation_pages.items():
        paginator = MagicMock()
        paginator.paginate.return_value = pages
        paginators[operation] = paginator
    client.get_paginator.side_effect = paginators.__getitem__
    client.paginators = paginators
    return client


def purchase_option(offer_id="offer-1", **overrides):
    value = {
        "purchaseOptionId": offer_id,
        "purchaseOptionType": "OFFER",
        "associatedEntities": [
            {
                "product": {"productId": DATADOG_OPERATOR_PRODUCT_ID},
                "offer": {"offerId": offer_id},
            }
        ],
    }
    value.update(overrides)
    return value


def free_offer(offer_id="offer-1", **overrides):
    value = {
        "offerId": offer_id,
        "agreementProposalId": "ap-proposal1",
        "associatedEntities": [
            {"product": {"productId": DATADOG_OPERATOR_PRODUCT_ID}}
        ],
        "pricingModel": {"pricingModelType": "FREE"},
        "badges": [],
    }
    value.update(overrides)
    return value


def event(request_type="Create"):
    return {
        "RequestType": request_type,
        "RequestId": "request-1",
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/id",
        "LogicalResourceId": "DatadogOperatorSubscriptionFunctionTrigger",
        "ResourceProperties": {
            "AccountId": "123456789012",
        },
    }


class TestTemplate(unittest.TestCase):
    def test_template_embeds_subscription_source(self):
        template = Path(__file__).with_name(
            "datadog_integration_permissions.yaml"
        ).read_text()

        self.assertEqual(template.count("<ACCEPT_OPERATOR_SUBSCRIPTION_SOURCE>"), 1)
        self.assertIn(
            "Conditions:\n"
            "  IncludeEKS:\n"
            "    Fn::And:\n"
            "      - Fn::Equals:\n"
            "          - !Ref AWS::Partition\n"
            "          - aws\n"
            "      - Fn::Not:\n",
            template,
        )
        self.assertIn(
            "  InstrumentationResourceTypes:\n    Type: CommaDelimitedList",
            template,
        )
        self.assertEqual(
            template.count(
                'NormalizedResourceTypes: !Join [",", '
                "!Ref InstrumentationResourceTypes]"
            ),
            2,
        )
        self.assertIn(
            'InstrumentationResourceTypes: !Join [",", '
            "!Ref InstrumentationResourceTypes]",
            template,
        )
        for resource in (
            "DatadogOperatorSubscriptionLambdaExecutionRole",
            "DatadogOperatorSubscriptionFunction",
            "DatadogOperatorSubscriptionFunctionTrigger",
        ):
            self.assertIn(f"  {resource}:\n", template)
        self.assertEqual(template.count("    Condition: IncludeEKS"), 3)

        role_template = Path(__file__).with_name(
            "datadog_integration_role.yaml"
        ).read_text()
        self.assertIn(
            "  InstrumentationResourceTypes:\n    Type: CommaDelimitedList",
            role_template,
        )
        self.assertIn(
            'InstrumentationResourceTypes: !Join [",", '
            "!Ref InstrumentationResourceTypes]",
            role_template,
        )

    def test_template_grants_only_required_marketplace_actions(self):
        template = Path(__file__).with_name(
            "datadog_integration_permissions.yaml"
        ).read_text()

        actions = (
            "ListPurchaseOptions",
            "GetOffer",
            "GetOfferTerms",
            "SearchAgreements",
            "CreateAgreementRequest",
            "AcceptAgreementRequest",
            "GetAgreementEntitlements",
        )
        for action in actions:
            self.assertEqual(template.count(f"aws-marketplace:{action}\n"), 1)
        self.assertNotIn("aws-marketplace:CancelAgreement", template)
        self.assertIn(DATADOG_OPERATOR_PRODUCT_ID, template)
        self.assertEqual(template.count('                  "Null":'), 3)

    def test_release_embeds_subscription_source(self):
        release = Path(__file__).with_name("release.sh").read_text()

        self.assertIn(
            "embed_python_source_with_common datadog_integration_permissions.yaml "
            "accept_operator_subscription.py ACCEPT_OPERATOR_SUBSCRIPTION_SOURCE "
            "marketplace_agreement_compat.py",
            release,
        )


class TestMarketplaceAgreementCompatibility(unittest.TestCase):
    def test_adds_missing_operations_and_paginators(self):
        session = MagicMock()
        runtime_shape = {"runtime": True}
        service_data = {
            "operations": {"SearchAgreements": {}},
            "shapes": {"ResourceId": runtime_shape},
        }
        paginator_config = {}
        session._session.get_service_data.return_value = service_data
        session._session.get_paginator_model.return_value._paginator_config = (
            paginator_config
        )

        applied = apply_marketplace_agreement_compatibility(session)

        self.assertTrue(applied)
        self.assertIn("CreateAgreementRequest", service_data["operations"])
        self.assertIn("AcceptAgreementRequest", service_data["operations"])
        self.assertIn("GetAgreementEntitlements", service_data["operations"])
        self.assertIn("CreateAgreementRequestInput", service_data["shapes"])
        self.assertIn("AcceptAgreementRequestOutput", service_data["shapes"])
        self.assertIn("GetAgreementEntitlementsInput", service_data["shapes"])
        self.assertIn("GetAgreementEntitlementsOutput", service_data["shapes"])
        self.assertIs(service_data["shapes"]["ResourceId"], runtime_shape)
        self.assertIn("GetAgreementEntitlements", paginator_config)
        self.assertIn("SearchAgreements", paginator_config)

    def test_preserves_runtime_model_when_definitions_are_available(self):
        session = MagicMock()
        operations = {
            "CreateAgreementRequest": {"runtime": True},
            "AcceptAgreementRequest": {"runtime": True},
            "GetAgreementEntitlements": {"runtime": True},
        }
        paginator_config = {
            "GetAgreementEntitlements": {"runtime": True},
            "SearchAgreements": {"runtime": True},
        }
        service_data = {"operations": operations.copy(), "shapes": {}}
        session._session.get_service_data.return_value = service_data
        session._session.get_paginator_model.return_value._paginator_config = (
            paginator_config.copy()
        )

        applied = apply_marketplace_agreement_compatibility(session)

        self.assertFalse(applied)
        self.assertEqual(service_data["operations"], operations)
        self.assertEqual(service_data["shapes"], {})

    def test_adds_only_missing_paginator_to_runtime_model(self):
        session = MagicMock()
        operations = {
            "CreateAgreementRequest": {"runtime": True},
            "AcceptAgreementRequest": {"runtime": True},
            "GetAgreementEntitlements": {"runtime": True},
        }
        shapes = {"RuntimeShape": {"runtime": True}}
        service_data = {"operations": operations.copy(), "shapes": shapes.copy()}
        search_paginator = {"runtime": True}
        paginator_config = {"SearchAgreements": search_paginator}
        session._session.get_service_data.return_value = service_data
        session._session.get_paginator_model.return_value._paginator_config = (
            paginator_config
        )

        applied = apply_marketplace_agreement_compatibility(session)

        self.assertTrue(applied)
        self.assertEqual(service_data["operations"], operations)
        self.assertEqual(service_data["shapes"], shapes)
        self.assertIn("GetAgreementEntitlements", paginator_config)
        self.assertIs(paginator_config["SearchAgreements"], search_paginator)


class TestAgreementDiscovery(unittest.TestCase):
    def test_returns_active_agreement(self):
        client = paginator_client(
            search_agreements=[
                {"agreementViewSummaries": [{"agreementId": "agreement-1"}]}
            ]
        )

        self.assertEqual(find_active_agreement(client), "agreement-1")
        client.paginators["search_agreements"].paginate.assert_called_once_with(
            catalog="AWSMarketplace",
            filters=[
                {"name": "PartyType", "values": ["Acceptor"]},
                {"name": "AgreementType", "values": ["PurchaseAgreement"]},
                {
                    "name": "ResourceIdentifier",
                    "values": [DATADOG_OPERATOR_PRODUCT_ID],
                },
                {"name": "Status", "values": ["ACTIVE"]},
            ],
        )

    def test_returns_none_when_no_active_agreement_exists(self):
        client = paginator_client(
            search_agreements=[{"agreementViewSummaries": []}]
        )

        self.assertIsNone(find_active_agreement(client))

    def test_rejects_multiple_active_agreements_across_pages(self):
        client = paginator_client(
            search_agreements=[
                {"agreementViewSummaries": [{"agreementId": "agreement-1"}]},
                {"agreementViewSummaries": [{"agreementId": "agreement-2"}]},
            ]
        )

        with self.assertRaisesRegex(SubscriptionError, "Multiple active"):
            find_active_agreement(client)


class TestOfferDiscovery(unittest.TestCase):
    def _client(self, options, offers):
        client = paginator_client(
            list_purchase_options=[{"purchaseOptions": options}]
        )
        client.get_offer.side_effect = lambda offerId: offers[offerId]
        return client

    def test_selects_only_public_free_offer(self):
        client = self._client(
            [purchase_option()],
            {"offer-1": free_offer()},
        )

        self.assertEqual(find_free_offer(client)["offerId"], "offer-1")
        client.paginators["list_purchase_options"].paginate.assert_called_once_with(
            filters=[
                {
                    "filterType": "PRODUCT_ID",
                    "filterValues": [DATADOG_OPERATOR_PRODUCT_ID],
                },
                {
                    "filterType": "PURCHASE_OPTION_TYPE",
                    "filterValues": ["OFFER"],
                },
            ]
        )

    def test_rejects_nonfree_and_badged_offers(self):
        client = self._client(
            [purchase_option("paid"), purchase_option("private")],
            {
                "paid": free_offer(
                    "paid", pricingModel={"pricingModelType": "CONTRACT"}
                ),
                "private": free_offer("private", badges=[{"value": "PRIVATE"}]),
            },
        )

        with self.assertRaisesRegex(SubscriptionError, "No public free"):
            find_free_offer(client)

    def test_rejects_multiple_free_offers(self):
        client = self._client(
            [purchase_option("offer-1"), purchase_option("offer-2")],
            {
                "offer-1": free_offer("offer-1"),
                "offer-2": free_offer("offer-2"),
            },
        )

        with self.assertRaisesRegex(SubscriptionError, "Multiple public free"):
            find_free_offer(client)

    def test_skips_unavailable_offer(self):
        now = datetime.now(timezone.utc)
        client = self._client(
            [purchase_option(availableFromTime=now + timedelta(hours=1))],
            {"offer-1": free_offer()},
        )

        with self.assertRaisesRegex(SubscriptionError, "unavailable=1"):
            find_free_offer(client, now=now)
        client.get_offer.assert_not_called()

    def test_rejects_purchase_option_for_another_product(self):
        option = purchase_option()
        option["associatedEntities"][0]["product"]["productId"] = "other"
        client = self._client([option], {"offer-1": free_offer()})

        with self.assertRaisesRegex(SubscriptionError, "expected product"):
            find_free_offer(client)


class TestOfferTerms(unittest.TestCase):
    def test_returns_stably_sorted_requested_terms(self):
        client = paginator_client(
            get_offer_terms=[
                {
                    "offerTerms": [
                        {"supportTerm": {"id": "term-support"}},
                        {"legalTerm": {"id": "term-legal"}},
                    ]
                }
            ]
        )

        self.assertEqual(
            requested_terms(client, "offer-1"),
            [{"id": "term-legal"}, {"id": "term-support"}],
        )

    def test_rejects_missing_or_unsupported_terms(self):
        missing = paginator_client(
            get_offer_terms=[
                {"offerTerms": [{"legalTerm": {"id": "term-legal"}}]}
            ]
        )
        unsupported = paginator_client(
            get_offer_terms=[
                {
                    "offerTerms": [
                        {"legalTerm": {"id": "term-legal"}},
                        {"fixedUpfrontPricingTerm": {"id": "term-price"}},
                    ]
                }
            ]
        )

        with self.assertRaisesRegex(SubscriptionError, "exactly one"):
            requested_terms(missing, "offer-1")
        with self.assertRaisesRegex(SubscriptionError, "unsupported term"):
            requested_terms(unsupported, "offer-1")


class TestQuoteValidation(unittest.TestCase):
    def test_accepts_only_zero_amounts(self):
        validate_zero_charge_summary(
            {
                "newAgreementValue": "0.00",
                "newAgreementValueAfterTax": "0",
                "estimatedTaxes": {
                    "totalAmount": "0",
                    "breakdown": [{"amount": "0.0"}],
                },
                "expectedCharges": [
                    {
                        "amount": "0",
                        "amountAfterTax": "0",
                        "estimatedTaxes": {"totalAmount": "0"},
                    }
                ],
                "itemizedCharges": [{"incrementalChargeAmount": "0"}],
            }
        )

    def test_rejects_nonzero_and_unknown_amounts(self):
        with self.assertRaisesRegex(SubscriptionError, "nonzero charge"):
            validate_zero_charge_summary({"newAgreementValue": "0.01"})
        with self.assertRaisesRegex(SubscriptionError, "no charge summary"):
            validate_zero_charge_summary(None)
        with self.assertRaisesRegex(SubscriptionError, "no value"):
            validate_zero_charge_summary({})

    def test_rejects_after_tax_amounts(self):
        with self.assertRaisesRegex(SubscriptionError, "amountAfterTax"):
            validate_zero_charge_summary(
                {
                    "newAgreementValue": "0",
                    "expectedCharges": [
                        {"amount": "0", "amountAfterTax": "0.01"}
                    ],
                }
            )

    def test_rejects_future_nested_amount_fields(self):
        with self.assertRaisesRegex(
            SubscriptionError,
            r"futureCharges\[0\]\.serviceFeeAmount",
        ):
            validate_zero_charge_summary(
                {
                    "newAgreementValue": "0",
                    "futureCharges": [{"serviceFeeAmount": "1"}],
                }
            )

    def test_ignores_nonmonetary_value_fields(self):
        validate_zero_charge_summary(
            {
                "newAgreementValue": "0",
                "selectorValue": "paid-plan",
                "metadata": {"referenceValue": "1"},
            }
        )

    def test_requires_known_charge_amounts(self):
        for summary in (
            {"newAgreementValue": "0", "expectedCharges": [{}]},
            {"newAgreementValue": "0", "itemizedCharges": [{}]},
        ):
            with self.subTest(summary=summary):
                with self.assertRaisesRegex(SubscriptionError, "no value"):
                    validate_zero_charge_summary(summary)


class TestAgreementCreation(unittest.TestCase):
    @patch("accept_operator_subscription.find_free_offer")
    @patch("accept_operator_subscription.requested_terms")
    def test_creates_validates_and_accepts_agreement(self, mock_terms, mock_offer):
        discovery = MagicMock()
        agreement = MagicMock()
        mock_offer.return_value = free_offer()
        mock_terms.return_value = [
            {"id": "term-legal"},
            {"id": "term-support"},
        ]
        agreement.create_agreement_request.return_value = {
            "agreementRequestId": "request-1",
            "chargeSummary": {"newAgreementValue": "0"},
        }
        agreement.accept_agreement_request.return_value = {
            "agreementId": "agreement-1"
        }

        self.assertEqual(
            create_and_accept_agreement(event(), discovery, agreement),
            "agreement-1",
        )
        create_call = agreement.create_agreement_request.call_args.kwargs
        self.assertEqual(create_call["agreementProposalIdentifier"], "ap-proposal1")
        self.assertEqual(create_call["intent"], "NEW")
        self.assertEqual(create_call["requestedTerms"], mock_terms.return_value)
        self.assertEqual(len(create_call["clientToken"]), 36)
        agreement.accept_agreement_request.assert_called_once_with(
            agreementRequestId="request-1"
        )

    @patch("accept_operator_subscription.find_active_agreement")
    @patch("accept_operator_subscription.find_free_offer")
    @patch("accept_operator_subscription.requested_terms")
    def test_recovers_ambiguous_acceptance(
        self,
        mock_terms,
        mock_offer,
        mock_find_active,
    ):
        mock_offer.return_value = free_offer()
        mock_terms.return_value = [{"id": "legal"}, {"id": "support"}]
        mock_find_active.return_value = "agreement-1"
        agreement = MagicMock()
        agreement.create_agreement_request.return_value = {
            "agreementRequestId": "request-1",
            "chargeSummary": {"newAgreementValue": "0"},
        }
        agreement.accept_agreement_request.side_effect = TimeoutError("timed out")

        self.assertEqual(
            create_and_accept_agreement(event(), MagicMock(), agreement),
            "agreement-1",
        )

    @patch("accept_operator_subscription.time.monotonic", side_effect=[0, 100])
    @patch("accept_operator_subscription.find_active_agreement")
    @patch("accept_operator_subscription.find_free_offer")
    @patch("accept_operator_subscription.requested_terms")
    def test_does_not_recover_when_acceptance_was_not_attempted(
        self,
        mock_terms,
        mock_offer,
        mock_find_active,
        _mock_monotonic,
    ):
        mock_offer.return_value = free_offer()
        mock_terms.return_value = [{"id": "legal"}, {"id": "support"}]
        agreement = MagicMock()
        agreement.create_agreement_request.return_value = {
            "agreementRequestId": "request-1",
            "chargeSummary": {"newAgreementValue": "0"},
        }

        with self.assertRaisesRegex(RuntimeError, "accept_agreement_request"):
            create_and_accept_agreement(
                event(),
                MagicMock(),
                agreement,
                deadline=120,
            )

        agreement.accept_agreement_request.assert_not_called()
        mock_find_active.assert_not_called()


class TestEntitlements(unittest.TestCase):
    def test_returns_only_operator_entitlement_across_pages(self):
        client = paginator_client(
            get_agreement_entitlements=[
                {
                    "agreementEntitlements": [
                        {"resource": {"id": "other"}, "status": "PROVISIONED"}
                    ]
                },
                {
                    "agreementEntitlements": [
                        {
                            "resource": {"id": DATADOG_OPERATOR_PRODUCT_ID},
                            "status": "PENDING",
                        }
                    ]
                },
            ]
        )

        self.assertEqual(
            entitlement_status(client, "agreement-1")["status"], "PENDING"
        )
        paginator = client.paginators["get_agreement_entitlements"]
        paginator.paginate.assert_called_once_with(agreementId="agreement-1")

    @patch("accept_operator_subscription.time.sleep")
    @patch("accept_operator_subscription.entitlement_status")
    def test_waits_until_provisioned(self, mock_status, mock_sleep):
        mock_status.side_effect = [
            {"status": "PENDING", "statusReasonCode": "PROVISIONING_IN_PROGRESS"},
            {"status": "PROVISIONED", "statusReasonCode": "AGREEMENT_ACTIVE"},
        ]

        wait_for_entitlement(MagicMock(), "agreement-1", attempts=2, delay=1)

        mock_sleep.assert_called_once_with(1)

    def test_fails_terminal_status_and_timeout(self):
        with patch(
            "accept_operator_subscription.entitlement_status",
            return_value={"status": "FAILED", "statusReasonCode": "PRODUCT_RESTRICTED"},
        ):
            with self.assertRaisesRegex(SubscriptionError, "FAILED"):
                wait_for_entitlement(MagicMock(), "agreement-1", attempts=1)
        with patch(
            "accept_operator_subscription.entitlement_status", return_value=None
        ):
            with self.assertRaisesRegex(SubscriptionError, "Timed out"):
                wait_for_entitlement(MagicMock(), "agreement-1", attempts=1)

    @patch("accept_operator_subscription.time.monotonic", return_value=70)
    def test_stops_before_deadline_without_starting_request(self, _mock_monotonic):
        client = MagicMock()

        with self.assertRaisesRegex(SubscriptionError, "Timed out"):
            wait_for_entitlement(client, "agreement-1", deadline=100)

        client.get_paginator.assert_not_called()

    @patch("accept_operator_subscription.time.sleep")
    @patch("accept_operator_subscription.entitlement_status")
    @patch("accept_operator_subscription.time.monotonic", side_effect=[0, 65])
    def test_preserves_last_status_when_next_request_exceeds_budget(
        self,
        _mock_monotonic,
        mock_status,
        mock_sleep,
    ):
        mock_status.return_value = {
            "status": "PENDING",
            "statusReasonCode": "PROVISIONING_IN_PROGRESS",
        }

        with self.assertRaises(SubscriptionError) as raised:
            wait_for_entitlement(
                MagicMock(),
                "agreement-1",
                attempts=2,
                delay=5,
                deadline=100,
            )

        self.assertEqual(raised.exception.reason, "entitlement_timeout")
        self.assertIn("status=PENDING", str(raised.exception))
        mock_status.assert_called_once()
        mock_sleep.assert_not_called()


class TestAPIFailureStages(unittest.TestCase):
    def assert_stage(self, expected_stage, operation):
        with self.assertRaises(SubscriptionError) as raised:
            operation()
        self.assertEqual(raised.exception.stage, expected_stage)
        self.assertEqual(raised.exception.reason, "aws_api_error")
        self.assertIn("AccessDenied", str(raised.exception))

    def test_agreement_discovery_failure(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("AccessDenied")

        self.assert_stage(
            "agreement_discovery", lambda: find_active_agreement(client)
        )

    @patch("accept_operator_subscription.time.monotonic", return_value=90)
    def test_agreement_discovery_stops_near_deadline(self, _mock_monotonic):
        client = paginator_client(
            search_agreements=[{"agreementViewSummaries": []}]
        )

        with self.assertRaisesRegex(SubscriptionError, "deadline is near"):
            find_active_agreement(client, deadline=100)

    def test_offer_discovery_failure(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("AccessDenied")

        self.assert_stage("offer_discovery", lambda: find_free_offer(client))

    def test_offer_terms_failure(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("AccessDenied")

        self.assert_stage(
            "offer_terms", lambda: requested_terms(client, "offer-1")
        )

    def test_request_creation_failure(self):
        agreement = MagicMock()
        agreement.create_agreement_request.side_effect = RuntimeError("AccessDenied")
        with (
            patch(
                "accept_operator_subscription.find_free_offer",
                return_value=free_offer(),
            ),
            patch(
                "accept_operator_subscription.requested_terms",
                return_value=[{"id": "legal"}, {"id": "support"}],
            ),
        ):
            self.assert_stage(
                "request_creation",
                lambda: create_and_accept_agreement(
                    event(), MagicMock(), agreement
                ),
            )

    def test_request_acceptance_failure(self):
        agreement = MagicMock()
        agreement.create_agreement_request.return_value = {
            "agreementRequestId": "request-1",
            "chargeSummary": {"newAgreementValue": "0"},
        }
        agreement.accept_agreement_request.side_effect = RuntimeError("AccessDenied")
        with (
            patch(
                "accept_operator_subscription.find_free_offer",
                return_value=free_offer(),
            ),
            patch(
                "accept_operator_subscription.requested_terms",
                return_value=[{"id": "legal"}, {"id": "support"}],
            ),
            patch(
                "accept_operator_subscription.find_active_agreement",
                return_value=None,
            ),
        ):
            self.assert_stage(
                "request_acceptance",
                lambda: create_and_accept_agreement(
                    event(), MagicMock(), agreement
                ),
            )


class TestClientInitialization(unittest.TestCase):
    @patch(
        "accept_operator_subscription.apply_marketplace_agreement_compatibility",
        return_value=False,
    )
    @patch("accept_operator_subscription.boto3.Session")
    def test_reports_runtime_without_marketplace_discovery(
        self, mock_session, _mock_compatibility
    ):
        mock_session.return_value.client.side_effect = RuntimeError("UnknownServiceError")

        with self.assertRaises(SubscriptionError) as raised:
            ensure_subscription(event())

        self.assertEqual(raised.exception.stage, "sdk_initialization")
        self.assertEqual(raised.exception.reason, "unsupported_sdk")
        self.assertIn("UnknownServiceError", str(raised.exception))


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.context.get_remaining_time_in_millis.return_value = 300_000
        sys.modules["cfnresponse"].send.reset_mock()

    def response(self):
        return sys.modules["cfnresponse"].send.call_args

    @patch("accept_operator_subscription.boto3.Session")
    def test_delete_retains_agreement_without_aws_calls(self, mock_session):
        handler(event(request_type="Delete"), self.context)

        mock_session.assert_not_called()
        self.assertEqual(self.response().args[2], "SUCCESS")
        self.assertEqual(
            self.response().kwargs["responseData"], {"AgreementRetained": True}
        )

    @patch("accept_operator_subscription.time.monotonic", return_value=100)
    @patch("accept_operator_subscription.ensure_subscription")
    def test_returns_agreement_details(self, mock_ensure, _mock_monotonic):
        mock_ensure.return_value = "agreement-1"

        handler(event(), self.context)

        self.assertEqual(self.response().args[2], "SUCCESS")
        self.assertEqual(
            self.response().kwargs["responseData"],
            {"AgreementId": "agreement-1"},
        )
        mock_ensure.assert_called_once_with(event(), deadline=385)

    @patch("accept_operator_subscription.ensure_subscription")
    def test_reports_actionable_failure(self, mock_ensure):
        mock_ensure.side_effect = SubscriptionError(
            "offer_discovery", "no_eligible_free_offer", "No free offer"
        )

        handler(event(), self.context)

        self.assertEqual(self.response().args[2], "FAILED")
        self.assertEqual(
            self.response().kwargs["responseData"], {"Message": "No free offer"}
        )


if __name__ == "__main__":
    unittest.main()
