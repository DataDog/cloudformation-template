# Lambda runtimes can lag newly released AWS operations. Add these API definitions
# to botocore so it retains its normal validation, signing, retries, and parsing.
# The definitions mirror the AWS Marketplace Agreement 2020-03-01 service model.
_SERVICE_MODEL_PATCH = {
    "operations": {
        "CreateAgreementRequest": {
            "name": "CreateAgreementRequest",
            "http": {"method": "POST", "requestUri": "/"},
            "input": {"shape": "CreateAgreementRequestInput"},
            "output": {"shape": "CreateAgreementRequestOutput"},
            "errors": [
                {"shape": "AccessDeniedException"},
                {"shape": "ValidationException"},
                {"shape": "ResourceNotFoundException"},
                {"shape": "ThrottlingException"},
                {"shape": "ServiceQuotaExceededException"},
                {"shape": "InternalServerException"},
                {"shape": "ConflictException"},
            ],
        },
        "AcceptAgreementRequest": {
            "name": "AcceptAgreementRequest",
            "http": {"method": "POST", "requestUri": "/"},
            "input": {"shape": "AcceptAgreementRequestInput"},
            "output": {"shape": "AcceptAgreementRequestOutput"},
            "errors": [
                {"shape": "AccessDeniedException"},
                {"shape": "ValidationException"},
                {"shape": "ResourceNotFoundException"},
                {"shape": "ThrottlingException"},
                {"shape": "InternalServerException"},
                {"shape": "ConflictException"},
            ],
        },
        "GetAgreementEntitlements": {
            "name": "GetAgreementEntitlements",
            "http": {"method": "POST", "requestUri": "/"},
            "input": {"shape": "GetAgreementEntitlementsInput"},
            "output": {"shape": "GetAgreementEntitlementsOutput"},
            "errors": [
                {"shape": "AccessDeniedException"},
                {"shape": "ValidationException"},
                {"shape": "ResourceNotFoundException"},
                {"shape": "ThrottlingException"},
                {"shape": "InternalServerException"},
            ],
            "readonly": True,
        },
    },
    "shapes": {
        "AcceptAgreementRequestInput": {
            "type": "structure",
            "required": ["agreementRequestId"],
            "members": {
                "agreementRequestId": {"shape": "AgreementRequestId"},
                "purchaseOrders": {"shape": "PurchaseOrders"},
            },
        },
        "AcceptAgreementRequestOutput": {
            "type": "structure",
            "members": {"agreementId": {"shape": "ResourceId"}},
        },
        "AccessDeniedException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
                "reason": {"shape": "AccessDeniedExceptionReason"},
            },
            "exception": True,
        },
        "AccessDeniedExceptionReason": {
            "type": "string",
            "enum": [
                "INVALID_ACCOUNT_STATE",
                "DENIED_BY_PRIVATE_MARKETPLACE_POLICY",
                "FAILED_KYC_COMPLIANCE",
                "MISSING_MFA",
                "INVALID_ACCESS",
            ],
        },
        "AgreementEntitlement": {
            "type": "structure",
            "members": {
                "resource": {"shape": "Resource"},
                "type": {"shape": "EntitlementType"},
                "registrationToken": {"shape": "RegistrationToken"},
                "status": {"shape": "AgreementEntitlementStatus"},
                "statusReasonCode": {"shape": "AgreementEntitlementStatusReasonCode"},
                "licenseArn": {"shape": "AwsArn"},
            },
        },
        "AgreementEntitlementList": {
            "type": "list",
            "member": {"shape": "AgreementEntitlement"},
        },
        "AgreementEntitlementStatus": {
            "type": "string",
            "enum": [
                "PROVISIONED",
                "SCHEDULED",
                "PENDING",
                "FAILED",
                "DEPROVISIONED",
            ],
        },
        "AgreementEntitlementStatusReasonCode": {
            "type": "string",
            "enum": [
                "PROVISIONING_IN_PROGRESS",
                "FUTURE_START_DATE",
                "INVALID_PAYMENT_INSTRUMENT",
                "INCOMPATIBLE_CURRENCY",
                "ACCOUNT_SUSPENDED",
                "UNSUPPORTED_OPERATION",
                "AGREEMENT_INACTIVE",
                "AGREEMENT_ACTIVE",
                "PRODUCT_RESTRICTED",
            ],
        },
        "AgreementProposalId": {
            "type": "string",
            "max": 64,
            "min": 1,
            "pattern": "(at-|ap-)[A-Za-z0-9]+",
        },
        "AgreementRequestId": {
            "type": "string",
            "max": 64,
            "min": 1,
            "pattern": "ar-[A-Za-z0-9]+",
        },
        "AgreementResourceType": {
            "type": "string",
            "max": 64,
            "min": 1,
            "pattern": "[a-zA-Z]+",
        },
        "AwsArn": {
            "type": "string",
            "max": 2048,
            "min": 1,
            "pattern": "arn:aws[a-zA-Z-]*:[A-Za-z0-9][A-Za-z0-9_/.-]{0,62}:"
            "[A-Za-z0-9_/.-]{0,63}:[A-Za-z0-9_/.-]{0,63}:"
            "[A-Za-z0-9][A-Za-z0-9:_/+=,@.-]{0,1023}",
        },
        "ChargeRevision": {"type": "long", "box": True, "min": 1},
        "ChargeSummary": {
            "type": "structure",
            "members": {
                "currencyCode": {"shape": "CurrencyCode"},
                "newAgreementValue": {"shape": "BoundedString"},
                "newAgreementValueAfterTax": {"shape": "BoundedString"},
                "expectedCharges": {"shape": "ExpectedChargeList"},
                "estimatedTaxes": {"shape": "EstimatedTaxes"},
                "itemizedCharges": {"shape": "ItemizedChargeList"},
                "invoicingEntity": {"shape": "InvoicingEntity"},
            },
        },
        "ConflictException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
                "resourceId": {"shape": "ResourceId"},
                "resourceType": {"shape": "ResourceType"},
            },
            "exception": True,
        },
        "CreateAgreementRequestInput": {
            "type": "structure",
            "required": ["intent", "requestedTerms"],
            "members": {
                "clientToken": {"shape": "ClientToken", "idempotencyToken": True},
                "intent": {"shape": "Intent"},
                "requestedTerms": {"shape": "RequestedTermList"},
                "sourceAgreementIdentifier": {"shape": "ResourceId"},
                "agreementProposalIdentifier": {"shape": "AgreementProposalId"},
                "taxConfiguration": {"shape": "TaxConfiguration"},
            },
        },
        "CreateAgreementRequestOutput": {
            "type": "structure",
            "members": {
                "agreementRequestId": {"shape": "AgreementRequestId"},
                "chargeSummary": {"shape": "ChargeSummary"},
            },
        },
        "EstimatedTaxes": {
            "type": "structure",
            "members": {
                "breakdown": {"shape": "TaxBreakdown"},
                "totalAmount": {"shape": "BoundedString"},
            },
        },
        "ExpectedCharge": {
            "type": "structure",
            "members": {
                "id": {"shape": "ResourceId"},
                "time": {"shape": "Timestamp"},
                "amount": {"shape": "BoundedString"},
                "amountAfterTax": {"shape": "BoundedString"},
                "timing": {"shape": "Timing"},
                "estimatedTaxes": {"shape": "EstimatedTaxes"},
            },
        },
        "EntitlementType": {
            "type": "string",
            "max": 64,
            "min": 1,
            "pattern": "[A-Za-z:]+",
        },
        "ExpectedChargeList": {"type": "list", "member": {"shape": "ExpectedCharge"}},
        "GetAgreementEntitlementsInput": {
            "type": "structure",
            "required": ["agreementId"],
            "members": {
                "agreementId": {"shape": "ResourceId"},
                "maxResults": {"shape": "MaxResults"},
                "nextToken": {"shape": "NextToken"},
            },
        },
        "GetAgreementEntitlementsOutput": {
            "type": "structure",
            "members": {
                "agreementEntitlements": {"shape": "AgreementEntitlementList"},
                "nextToken": {"shape": "NextToken"},
            },
        },
        "Integer": {"type": "integer", "box": True},
        "Intent": {"type": "string", "enum": ["NEW", "AMEND", "REPLACE"]},
        "InternalServerException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
            },
            "exception": True,
            "fault": True,
        },
        "InvoicingEntity": {
            "type": "structure",
            "members": {
                "legalName": {"shape": "BoundedString"},
                "branchName": {"shape": "BoundedString"},
            },
        },
        "ItemizedCharge": {
            "type": "structure",
            "members": {
                "dimensionKey": {"shape": "BoundedString"},
                "newQuantity": {"shape": "Integer"},
                "oldQuantity": {"shape": "Integer"},
                "chargeReference": {"shape": "ResourceId"},
                "incrementalChargeAmount": {"shape": "BoundedString"},
            },
        },
        "ItemizedChargeList": {"type": "list", "member": {"shape": "ItemizedCharge"}},
        "MaxResults": {"type": "integer", "box": True, "max": 50, "min": 1},
        "NextToken": {
            "type": "string",
            "max": 8192,
            "min": 0,
            "pattern": "[a-zA-Z0-9+/=_-]+",
        },
        "PurchaseOrder": {
            "type": "structure",
            "required": ["chargeId"],
            "members": {
                "chargeId": {"shape": "ResourceId"},
                "chargeRevision": {"shape": "ChargeRevision"},
                "agreementId": {"shape": "ResourceId"},
                "purchaseOrderReference": {"shape": "PurchaseOrderReference"},
            },
        },
        "PurchaseOrderReference": {"type": "string", "min": 1},
        "PurchaseOrders": {
            "type": "list",
            "member": {"shape": "PurchaseOrder"},
            "max": 86,
            "min": 1,
        },
        "RegistrationToken": {
            "type": "string",
            "max": 512,
            "min": 1,
            "pattern": "[A-Za-z0-9+/=.:_-]+",
        },
        "RequestedTerm": {
            "type": "structure",
            "required": ["id"],
            "members": {
                "id": {"shape": "TermId"},
                "configuration": {"shape": "RequestedTermConfiguration"},
            },
        },
        "RequestedTermConfiguration": {
            "type": "structure",
            "members": {
                "configurableUpfrontPricingTermConfiguration": {
                    "shape": "ConfigurableUpfrontPricingTermConfiguration"
                },
                "renewalTermConfiguration": {"shape": "RenewalTermConfiguration"},
                "variablePaymentTermConfiguration": {
                    "shape": "VariablePaymentTermConfiguration"
                },
            },
            "union": True,
        },
        "RequestedTermList": {
            "type": "list",
            "member": {"shape": "RequestedTerm"},
            "max": 30,
            "min": 1,
        },
        "Resource": {
            "type": "structure",
            "members": {
                "id": {"shape": "ResourceId"},
                "type": {"shape": "AgreementResourceType"},
            },
        },
        "ResourceNotFoundException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
                "resourceId": {"shape": "ResourceId"},
                "resourceType": {"shape": "ResourceType"},
            },
            "exception": True,
        },
        "ResourceType": {
            "type": "string",
            "enum": [
                "Agreement",
                "AgreementRequest",
                "AgreementProposal",
                "Charge",
                "PaymentRequest",
                "Invoice",
                "AgreementCancellationRequest",
                "BillingAdjustmentRequest",
            ],
        },
        "ServiceQuotaExceededException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
                "quotaCode": {"shape": "BoundedString"},
                "serviceCode": {"shape": "BoundedString"},
                "resourceType": {"shape": "BoundedString"},
                "resourceId": {"shape": "ResourceId"},
            },
            "exception": True,
        },
        "TaxBreakdown": {"type": "list", "member": {"shape": "TaxBreakdownItem"}},
        "TaxBreakdownItem": {
            "type": "structure",
            "members": {
                "amount": {"shape": "BoundedString"},
                "rate": {"shape": "BoundedString"},
                "type": {"shape": "BoundedString"},
            },
        },
        "TaxConfiguration": {
            "type": "structure",
            "members": {"taxEstimation": {"shape": "TaxEstimation"}},
        },
        "TaxEstimation": {"type": "string", "enum": ["DISABLED", "ENABLED"]},
        "ThrottlingException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
            },
            "exception": True,
        },
        "Timing": {
            "type": "string",
            "enum": ["ON_ACCEPTANCE", "SCHEDULED", "BILLING_PERIOD"],
        },
        "ValidationException": {
            "type": "structure",
            "members": {
                "requestId": {"shape": "RequestId"},
                "message": {"shape": "ExceptionMessage"},
                "reason": {"shape": "ValidationExceptionReason"},
                "fields": {"shape": "ValidationExceptionFieldList"},
            },
            "exception": True,
        },
        "ValidationExceptionReason": {
            "type": "string",
            "enum": [
                "MISSING_BILLING_ADJUSTMENTS",
                "BILLING_ADJUSTMENTS_LIMIT_EXCEEDED",
                "MISSING_INVOICE_ID",
                "INVALID_ADJUSTMENT_AMOUNT",
                "MISSING_ADJUSTMENT_AMOUNT",
                "INVALID_REASON_CODE",
                "MISSING_REASON_CODE",
                "MISSING_DESCRIPTION",
                "INVALID_INVOICE_ADJUSTMENT_PERIOD",
                "INVALID_CURRENCY_CODE",
                "MISSING_CURRENCY_CODE",
                "EXCEEDED_MAXIMUM_ADJUSTMENT_AMOUNT",
                "MISSING_BILLING_ADJUSTMENT_REQUEST_ENTRY",
                "MULTIPLE_AGREEMENT_IDS",
                "INVALID_AGREEMENT_CANCELLATION_REQUEST_ID",
                "MISSING_AGREEMENT_CANCELLATION_REQUEST_ID",
                "MISSING_REASON",
                "INVALID_REASON",
                "INVALID_STATUS",
                "INVALID_AGREEMENT_ID",
                "MISSING_AGREEMENT_ID",
                "INVALID_CATALOG",
                "INVALID_FILTERS",
                "INVALID_FILTER_NAME",
                "MISSING_FILTER_NAME",
                "INVALID_FILTER_VALUES",
                "MISSING_FILTER_VALUES",
                "INVALID_SORT_BY",
                "INVALID_SORT_ORDER",
                "INVALID_NEXT_TOKEN",
                "INVALID_MAX_RESULTS",
                "INVALID_TERM_ID",
                "MISSING_TERM_ID",
                "MISSING_NAME",
                "INVALID_NAME",
                "INVALID_DESCRIPTION",
                "MISSING_CHARGE_AMOUNT",
                "INVALID_CHARGE_AMOUNT",
                "MISSING_PAYMENT_REQUEST_ID",
                "INVALID_PAYMENT_REQUEST_ID",
                "MISSING_PARTY_TYPE",
                "INVALID_PARTY_TYPE",
                "UNSUPPORTED_FILTERS",
                "INVALID_CLIENT_TOKEN",
                "INVALID_INTENT",
                "MISSING_INTENT",
                "INVALID_SOURCE_AGREEMENT_IDENTIFIER",
                "MISSING_SOURCE_AGREEMENT_IDENTIFIER",
                "INVALID_AGREEMENT_PROPOSAL_IDENTIFIER",
                "MISSING_AGREEMENT_PROPOSAL_IDENTIFIER",
                "INVALID_REQUESTED_TERMS",
                "MISSING_REQUESTED_TERMS",
                "INVALID_REQUESTED_TERM_ID",
                "MISSING_REQUESTED_TERM_ID",
                "INVALID_REQUESTED_TERM_CONFIGURATION",
                "MISSING_REQUESTED_TERM_CONFIGURATION",
                "INVALID_AGREEMENT_REQUEST_ID",
                "MISSING_AGREEMENT_REQUEST_ID",
                "INVALID_PURCHASE_ORDERS",
                "MISSING_PURCHASE_ORDERS",
                "INVALID_CHARGE_ID",
                "MISSING_CHARGE_ID",
                "INVALID_CHARGE_REVISION",
                "MISSING_CHARGE_REVISION",
                "INVALID_AGREEMENT_TYPE",
                "INVALID_PURCHASE_ORDER_REFERENCE",
                "INACTIVE_AGREEMENT",
                "SUPERSEDED_AGREEMENT_PROPOSAL",
                "EXPIRED_AGREEMENT_PROPOSAL",
                "MISSING_MANDATORY_TERMS",
                "INCOMPATIBLE_TERMS",
                "MISSING_USAGE_AGREEMENT",
                "INVALID_INCREMENTAL_CHARGE",
                "MISSING_ACCOUNT_ADDRESS",
                "UNSUPPORTED_ACTION",
                "INVALID_REJECTION_REASON",
                "INVALID_PAYMENT_REQUEST_STATUS",
                "OTHER",
                "DUPLICATE_CHARGES",
                "UNSUPPORTED_ACCOUNT_PLAN",
                "DUPLICATE_AGREEMENT_IN_ORGANIZATION",
                "MISSING_PURCHASE_ORDER_REFERENCE",
            ],
        },
    },
}

_PAGINATOR_MODEL_PATCH = {
    "GetAgreementEntitlements": {
        "input_token": "nextToken",
        "output_token": "nextToken",
        "limit_key": "maxResults",
        "result_key": "agreementEntitlements",
    },
    "SearchAgreements": {
        "input_token": "nextToken",
        "output_token": "nextToken",
        "limit_key": "maxResults",
        "result_key": "agreementViewSummaries",
    },
}


def apply_marketplace_agreement_compatibility(session):
    # boto3 has no public API for amending a service model before client construction.
    service_data = session._session.get_service_data("marketplace-agreement")
    paginator_config = session._session.get_paginator_model(
        "marketplace-agreement"
    )._paginator_config
    missing_operations = (
        _SERVICE_MODEL_PATCH["operations"].keys()
        - service_data["operations"].keys()
    )
    missing_paginators = _PAGINATOR_MODEL_PATCH.keys() - paginator_config.keys()
    if not missing_operations and not missing_paginators:
        return False
    if missing_operations:
        service_data["operations"].update(
            {
                name: _SERVICE_MODEL_PATCH["operations"][name]
                for name in missing_operations
            }
        )
        for name, shape in _SERVICE_MODEL_PATCH["shapes"].items():
            service_data["shapes"].setdefault(name, shape)
    paginator_config.update(
        {name: _PAGINATOR_MODEL_PATCH[name] for name in missing_paginators}
    )
    return True
