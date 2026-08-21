import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3
from botocore.config import Config
import cfnresponse


LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

DATADOG_OPERATOR_PRODUCT_ID = "6e852b2a-ecbb-431c-9b63-7de0288f4d00"
MARKETPLACE_CATALOG = "AWSMarketplace"
MARKETPLACE_REGION = "us-east-1"
ENTITLEMENT_ATTEMPTS = 24
ENTITLEMENT_DELAY_SECONDS = 5
AWS_READ_TIMEOUT_SECONDS = 20
SDK_CONNECT_TIMEOUT_SECONDS = 5
SDK_READ_TIMEOUT_SECONDS = 10
SDK_REQUEST_BUDGET_SECONDS = 32
CLOUDFORMATION_RESPONSE_BUFFER_SECONDS = 15

AWS_CONFIG = Config(
    retries={"total_max_attempts": 2, "mode": "standard"},
    connect_timeout=SDK_CONNECT_TIMEOUT_SECONDS,
    read_timeout=SDK_READ_TIMEOUT_SECONDS,
)


class SubscriptionError(Exception):
    def __init__(
        self,
        stage,
        reason,
        message,
        *,
        offer_id=None,
        agreement_request_id=None,
        agreement_id=None,
    ):
        super().__init__(message)
        self.stage = stage
        self.reason = reason
        self.offer_id = offer_id
        self.agreement_request_id = agreement_request_id
        self.agreement_id = agreement_id


def _physical_resource_id(event):
    return event.get("PhysicalResourceId") or (
        f"{event['StackId']}/{event['LogicalResourceId']}"
    )


def _send_response(event, context, status, data):
    cfnresponse.send(
        event,
        context,
        status,
        responseData=data,
        physicalResourceId=_physical_resource_id(event),
    )


def _log(stage, result, reason, **fields):
    payload = {
        "marketplace_stage": stage,
        "marketplace_result": result,
        "marketplace_reason": reason,
        "marketplace_control_plane_region": MARKETPLACE_REGION,
        **{key: value for key, value in fields.items() if value is not None},
    }
    LOGGER.info(
        "Datadog Operator Marketplace subscription event %s",
        json.dumps(payload, default=str, sort_keys=True),
    )


def _require_sdk_request_budget(deadline, operation):
    if (
        deadline is not None
        and time.monotonic() + SDK_REQUEST_BUDGET_SECONDS >= deadline
    ):
        raise RuntimeError(
            f"{operation} was not attempted because the Lambda deadline is near"
        )


def _pages(
    client,
    operation,
    result_key,
    *,
    error_stage,
    error_message,
    deadline=None,
    **kwargs,
):
    try:
        paginator = iter(client.get_paginator(operation).paginate(**kwargs))
        while True:
            _require_sdk_request_budget(deadline, operation)
            try:
                page = next(paginator)
            except StopIteration:
                return
            yield from page.get(result_key, [])
    except SubscriptionError:
        raise
    except Exception as error:
        raise SubscriptionError(
            error_stage,
            "aws_api_error",
            f"{error_message}: {error}",
        ) from error


def _api_call(stage, message, operation, *, deadline=None, **kwargs):
    try:
        _require_sdk_request_budget(
            deadline,
            getattr(operation, "__name__", "AWS API call"),
        )
        return operation(**kwargs)
    except Exception as error:
        raise SubscriptionError(stage, "aws_api_error", f"{message}: {error}") from error


def find_active_agreement(agreement_client, *, deadline=None):
    filters = [
        {"name": "PartyType", "values": ["Acceptor"]},
        {"name": "AgreementType", "values": ["PurchaseAgreement"]},
        {
            "name": "ResourceIdentifier",
            "values": [DATADOG_OPERATOR_PRODUCT_ID],
        },
        {"name": "Status", "values": ["ACTIVE"]},
    ]
    agreements = list(
        _pages(
            agreement_client,
            "search_agreements",
            "agreementViewSummaries",
            error_stage="agreement_discovery",
            error_message="Failed to search for an active Datadog Operator Marketplace agreement",
            deadline=deadline,
            catalog=MARKETPLACE_CATALOG,
            filters=filters,
        )
    )
    if not agreements:
        return None
    if len(agreements) != 1:
        raise SubscriptionError(
            "agreement_discovery",
            "multiple_active_agreements",
            "Multiple active Datadog Operator Marketplace agreements were returned",
        )

    agreement_id = agreements[0].get("agreementId")
    if not agreement_id:
        raise SubscriptionError(
            "agreement_discovery",
            "invalid_response",
            "The active Datadog Operator Marketplace agreement has no identifier",
        )
    return agreement_id


def _is_available(resource, now):
    available_from = resource.get("availableFromTime")
    expiration = resource.get("expirationTime")
    return (available_from is None or now >= available_from) and (
        expiration is None or now < expiration
    )


def _validate_purchase_option(option):
    offer_id = option.get("purchaseOptionId")
    if not offer_id or option.get("purchaseOptionType") != "OFFER":
        raise SubscriptionError(
            "offer_discovery",
            "invalid_offer",
            "The Datadog Operator Marketplace purchase option is not an offer",
            offer_id=offer_id,
        )

    entities = option.get("associatedEntities", [])
    if len(entities) != 1:
        raise SubscriptionError(
            "offer_discovery",
            "invalid_offer",
            "The Datadog Operator Marketplace purchase option has unexpected associated entities",
            offer_id=offer_id,
        )
    entity = entities[0]
    if (
        entity.get("product", {}).get("productId")
        != DATADOG_OPERATOR_PRODUCT_ID
        or entity.get("offer", {}).get("offerId") != offer_id
    ):
        raise SubscriptionError(
            "offer_discovery",
            "invalid_offer",
            "The Datadog Operator Marketplace purchase option does not match "
            "the expected product and offer",
            offer_id=offer_id,
        )
    return offer_id


def _validate_offer(offer_id, offer):
    entities = offer.get("associatedEntities", [])
    if (
        offer.get("offerId") != offer_id
        or not offer.get("agreementProposalId")
        or len(entities) != 1
        or entities[0].get("product", {}).get("productId")
        != DATADOG_OPERATOR_PRODUCT_ID
        or not offer.get("pricingModel")
    ):
        raise SubscriptionError(
            "offer_discovery",
            "invalid_offer",
            "The Datadog Operator Marketplace offer details are incomplete "
            "or do not match the expected product",
            offer_id=offer_id,
        )


def find_free_offer(discovery_client, now=None, *, deadline=None):
    now = now or datetime.now(timezone.utc)
    candidates = _pages(
        discovery_client,
        "list_purchase_options",
        "purchaseOptions",
        error_stage="offer_discovery",
        error_message="Failed to list Datadog Operator Marketplace purchase options",
        deadline=deadline,
        filters=[
            {
                "filterType": "PRODUCT_ID",
                "filterValues": [DATADOG_OPERATOR_PRODUCT_ID],
            },
            {"filterType": "PURCHASE_OPTION_TYPE", "filterValues": ["OFFER"]},
        ],
    )

    free_offers = []
    candidate_count = 0
    unavailable_count = 0
    ineligible_count = 0
    for option in candidates:
        candidate_count += 1
        offer_id = _validate_purchase_option(option)
        if not _is_available(option, now):
            unavailable_count += 1
            continue

        offer = _api_call(
            "offer_discovery",
            f"Failed to get Datadog Operator Marketplace offer {offer_id}",
            discovery_client.get_offer,
            deadline=deadline,
            offerId=offer_id,
        )
        _validate_offer(offer_id, offer)
        if not _is_available(offer, now):
            unavailable_count += 1
            continue
        if (
            offer["pricingModel"].get("pricingModelType") != "FREE"
            or offer.get("badges")
        ):
            ineligible_count += 1
            continue
        free_offers.append(offer)

    if not free_offers:
        raise SubscriptionError(
            "offer_discovery",
            "no_eligible_free_offer",
            "No public free Datadog Operator Marketplace offer was returned "
            f"(candidates={candidate_count} unavailable={unavailable_count} "
            f"nonfree_or_badged={ineligible_count})",
        )
    if len(free_offers) != 1:
        raise SubscriptionError(
            "offer_discovery",
            "multiple_free_offers",
            "Multiple public free Datadog Operator Marketplace offers were returned",
        )
    return free_offers[0]


def requested_terms(discovery_client, offer_id, *, deadline=None):
    terms = list(
        _pages(
            discovery_client,
            "get_offer_terms",
            "offerTerms",
            error_stage="offer_terms",
            error_message=f"Failed to get Datadog Operator Marketplace offer {offer_id} terms",
            deadline=deadline,
            offerId=offer_id,
        )
    )
    term_ids = []
    seen = set()
    supported = {"legalTerm", "supportTerm"}
    for term in terms:
        present = [name for name in supported if name in term]
        if len(present) != 1 or len(term) != 1:
            raise SubscriptionError(
                "offer_terms",
                "unsupported_terms",
                "The free Datadog Operator Marketplace offer contains an unsupported term",
                offer_id=offer_id,
            )
        term_name = present[0]
        if term_name in seen:
            raise SubscriptionError(
                "offer_terms",
                "invalid_terms",
                f"The Datadog Operator Marketplace offer contains multiple {term_name} values",
                offer_id=offer_id,
            )
        term_id = term[term_name].get("id")
        if not term_id:
            raise SubscriptionError(
                "offer_terms",
                "invalid_terms",
                "The Datadog Operator Marketplace offer contains a term without an identifier",
                offer_id=offer_id,
            )
        seen.add(term_name)
        term_ids.append(term_id)

    if seen != supported:
        raise SubscriptionError(
            "offer_terms",
            "invalid_terms",
            "The Datadog Operator Marketplace offer must contain exactly one "
            "legal and one support term",
            offer_id=offer_id,
        )
    return [{"id": term_id} for term_id in sorted(term_ids)]


def _require_zero(value, field, *, required=False):
    if value is None:
        if required:
            raise SubscriptionError(
                "quote_validation",
                "unknown_quote_amount",
                f"The free Datadog Operator Marketplace quote has no value for {field}",
            )
        return
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SubscriptionError(
            "quote_validation",
            "unknown_quote_amount",
            f"The free Datadog Operator Marketplace quote has an invalid value for {field}",
        ) from error
    if amount != 0:
        raise SubscriptionError(
            "quote_validation",
            "nonzero_quote",
            f"The free Datadog Operator Marketplace quote contains a nonzero charge at {field}",
        )


def _validate_amount_fields(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            field = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                _validate_amount_fields(value, field)
            elif key.lower().endswith(("amount", "amountaftertax")):
                _require_zero(value, field)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _validate_amount_fields(item, f"{path}[{index}]")


def validate_zero_charge_summary(summary):
    if summary is None:
        raise SubscriptionError(
            "quote_validation",
            "unknown_quote_amount",
            "The free Datadog Operator Marketplace agreement quote has no charge summary",
        )

    _require_zero(
        summary.get("newAgreementValue"),
        "newAgreementValue",
        required=True,
    )
    _require_zero(
        summary.get("newAgreementValueAfterTax"),
        "newAgreementValueAfterTax",
    )
    for index, charge in enumerate(summary.get("expectedCharges", [])):
        _require_zero(
            charge.get("amount"),
            f"expectedCharges[{index}].amount",
            required=True,
        )
    for index, charge in enumerate(summary.get("itemizedCharges", [])):
        _require_zero(
            charge.get("incrementalChargeAmount"),
            f"itemizedCharges[{index}].incrementalChargeAmount",
            required=True,
        )
    _validate_amount_fields(summary)


def _client_token(event, proposal_id, terms):
    seed = "\0".join(
        [
            event["StackId"],
            event["LogicalResourceId"],
            event["RequestId"],
            proposal_id,
            *(term["id"] for term in terms),
        ]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_OID, seed))


def create_and_accept_agreement(
    event,
    discovery_client,
    agreement_client,
    *,
    deadline=None,
):
    offer = find_free_offer(discovery_client, deadline=deadline)
    offer_id = offer["offerId"]
    terms = requested_terms(discovery_client, offer_id, deadline=deadline)
    response = _api_call(
        "request_creation",
        "Failed to create the Datadog Operator Marketplace agreement request",
        agreement_client.create_agreement_request,
        deadline=deadline,
        agreementProposalIdentifier=offer["agreementProposalId"],
        clientToken=_client_token(event, offer["agreementProposalId"], terms),
        intent="NEW",
        requestedTerms=terms,
    )
    try:
        validate_zero_charge_summary(response.get("chargeSummary"))
    except SubscriptionError as error:
        error.offer_id = offer_id
        raise
    agreement_request_id = response.get("agreementRequestId")
    if not agreement_request_id:
        raise SubscriptionError(
            "request_creation",
            "invalid_response",
            "The Datadog Operator Marketplace agreement request has no identifier",
            offer_id=offer_id,
        )
    _log(
        "request_creation",
        "succeeded",
        "free_quote_validated",
        marketplace_offer_id=offer_id,
        marketplace_agreement_request_id=agreement_request_id,
    )

    try:
        _require_sdk_request_budget(deadline, "accept_agreement_request")
        accepted = agreement_client.accept_agreement_request(
            agreementRequestId=agreement_request_id
        )
    except Exception as acceptance_error:
        try:
            agreement_id = find_active_agreement(
                agreement_client,
                deadline=deadline,
            )
        except Exception as recovery_error:
            raise SubscriptionError(
                "acceptance_recovery",
                "recovery_failed",
                "Failed to accept the Datadog Operator Marketplace agreement "
                f"request and could not determine whether it succeeded: {recovery_error}",
                offer_id=offer_id,
                agreement_request_id=agreement_request_id,
            ) from acceptance_error
        if agreement_id:
            _log(
                "request_acceptance",
                "succeeded",
                "active_agreement_recovered",
                marketplace_offer_id=offer_id,
                marketplace_agreement_request_id=agreement_request_id,
                marketplace_agreement_id=agreement_id,
            )
            return agreement_id
        raise SubscriptionError(
            "request_acceptance",
            "aws_api_error",
            "Failed to accept the Datadog Operator Marketplace agreement "
            f"request: {acceptance_error}",
            offer_id=offer_id,
            agreement_request_id=agreement_request_id,
        ) from acceptance_error

    agreement_id = accepted.get("agreementId")
    if not agreement_id:
        raise SubscriptionError(
            "request_acceptance",
            "invalid_response",
            "The accepted Datadog Operator Marketplace agreement has no identifier",
            offer_id=offer_id,
            agreement_request_id=agreement_request_id,
        )
    _log(
        "request_acceptance",
        "succeeded",
        "agreement_accepted",
        marketplace_offer_id=offer_id,
        marketplace_agreement_request_id=agreement_request_id,
        marketplace_agreement_id=agreement_id,
    )
    return agreement_id


def entitlement_status(agreement_client, agreement_id, *, deadline=None):
    matches = [
        entitlement
        for entitlement in _pages(
            agreement_client,
            "get_agreement_entitlements",
            "agreementEntitlements",
            error_stage="entitlement",
            error_message=(
                "Failed to get Datadog Operator Marketplace agreement "
                f"{agreement_id} entitlements"
            ),
            deadline=deadline,
            agreementId=agreement_id,
        )
        if entitlement.get("resource", {}).get("id")
        == DATADOG_OPERATOR_PRODUCT_ID
    ]
    if len(matches) > 1:
        raise SubscriptionError(
            "entitlement",
            "invalid_response",
            "Multiple Datadog Operator Marketplace entitlements were returned",
            agreement_id=agreement_id,
        )
    return matches[0] if matches else None


def wait_for_entitlement(
    agreement_client,
    agreement_id,
    *,
    attempts=ENTITLEMENT_ATTEMPTS,
    delay=ENTITLEMENT_DELAY_SECONDS,
    deadline=None,
):
    last_status = None
    last_reason = None
    for attempt in range(attempts):
        if (
            deadline is not None
            and time.monotonic() + AWS_READ_TIMEOUT_SECONDS >= deadline
        ):
            break
        entitlement = entitlement_status(
            agreement_client, agreement_id, deadline=deadline
        )
        status = entitlement.get("status") if entitlement else None
        reason = entitlement.get("statusReasonCode") if entitlement else None
        last_status = status
        last_reason = reason
        if status == "PROVISIONED":
            _log(
                "entitlement",
                "succeeded",
                "entitlement_provisioned",
                marketplace_agreement_id=agreement_id,
                marketplace_entitlement_status=status,
                marketplace_entitlement_reason=reason,
            )
            return
        if status in {"FAILED", "DEPROVISIONED"}:
            raise SubscriptionError(
                "entitlement",
                "entitlement_failed",
                f"The Datadog Operator Marketplace entitlement is {status} ({reason})",
                agreement_id=agreement_id,
            )
        if status not in {None, "PENDING", "SCHEDULED"}:
            raise SubscriptionError(
                "entitlement",
                "unsupported_entitlement_status",
                f"The Datadog Operator Marketplace entitlement has unsupported status {status}",
                agreement_id=agreement_id,
            )
        if attempt + 1 < attempts:
            if (
                deadline is not None
                and time.monotonic() + delay + AWS_READ_TIMEOUT_SECONDS >= deadline
            ):
                break
            time.sleep(delay)

    raise SubscriptionError(
        "entitlement",
        "entitlement_timeout",
        "Timed out waiting for the Datadog Operator Marketplace entitlement "
        f"(status={last_status} reason={last_reason})",
        agreement_id=agreement_id,
    )


def ensure_subscription(event, *, deadline=None):
    try:
        session = boto3.Session()
        discovery_client = session.client(
            "marketplace-discovery",
            region_name=MARKETPLACE_REGION,
            config=AWS_CONFIG,
        )
        agreement_client = session.client(
            "marketplace-agreement",
            region_name=MARKETPLACE_REGION,
            config=AWS_CONFIG,
        )
    except Exception as error:
        raise SubscriptionError(
            "sdk_initialization",
            "unsupported_sdk",
            "The Lambda runtime AWS SDK could not initialize the required Marketplace "
            f"clients (boto3={getattr(boto3, '__version__', 'unknown')}): {error}",
        ) from error
    _log(
        "sdk_initialization",
        "succeeded",
        "clients_created",
        boto3_version=getattr(boto3, "__version__", "unknown"),
    )

    agreement_id = find_active_agreement(agreement_client, deadline=deadline)
    if agreement_id:
        _log(
            "agreement_discovery",
            "succeeded",
            "active_agreement_reused",
            marketplace_agreement_id=agreement_id,
        )
    else:
        _log(
            "agreement_discovery",
            "succeeded",
            "active_agreement_not_found",
        )
        agreement_id = create_and_accept_agreement(
            event,
            discovery_client,
            agreement_client,
            deadline=deadline,
        )
    wait_for_entitlement(agreement_client, agreement_id, deadline=deadline)
    return agreement_id


def handler(event, context):
    request_type = event["RequestType"]
    properties = event["ResourceProperties"]
    account_id = properties.get("AccountId")
    partition = properties.get("Partition", "aws")
    if request_type == "Delete":
        _log(
            "cloudformation_delete",
            "succeeded",
            "agreement_retained",
            account_id=account_id,
        )
        _send_response(event, context, cfnresponse.SUCCESS, {"AgreementRetained": True})
        return

    if partition != "aws":
        error = SubscriptionError(
            "partition_validation",
            "unsupported_partition",
            "Datadog Operator Marketplace subscription acceptance is supported "
            "only in the commercial AWS partition",
        )
        _log(
            error.stage,
            "failed",
            error.reason,
            account_id=account_id,
            error=str(error),
        )
        _send_response(event, context, cfnresponse.FAILED, {"Message": str(error)})
        return

    try:
        remaining_seconds = context.get_remaining_time_in_millis() / 1000
        deadline = time.monotonic() + max(
            0,
            remaining_seconds - CLOUDFORMATION_RESPONSE_BUFFER_SECONDS,
        )
        agreement_id = ensure_subscription(event, deadline=deadline)
        _send_response(
            event,
            context,
            cfnresponse.SUCCESS,
            {"AgreementId": agreement_id},
        )
    except Exception as error:
        stage = getattr(error, "stage", "subscription")
        reason = getattr(error, "reason", "aws_api_error")
        _log(
            stage,
            "failed",
            reason,
            account_id=account_id,
            marketplace_offer_id=getattr(error, "offer_id", None),
            marketplace_agreement_request_id=getattr(
                error, "agreement_request_id", None
            ),
            marketplace_agreement_id=getattr(error, "agreement_id", None),
            error=str(error),
        )
        LOGGER.exception("Failed to accept the Datadog Operator Marketplace agreement")
        _send_response(event, context, cfnresponse.FAILED, {"Message": str(error)})
