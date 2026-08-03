"""AWS price discovery and development architecture cost review."""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from workflow_infrastructure.development_environment.aws import DevelopmentAwsClient
from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


class ClockProtocol(Protocol):
    """UTC clock required by cost-review records."""

    def now(self) -> datetime:
        """Return the current timezone-aware instant.

        Returns:
            The current timezone-aware instant.
        """


class DevelopmentCostReviewer:
    """Own price discovery and one reproducible development cost checkpoint."""

    def __init__(
        self,
        *,
        aws: DevelopmentAwsClient,
        clock: ClockProtocol,
        project_root_path: Path,
        region: str,
    ) -> None:
        """Bind review output to one project and AWS region.

        Args:
            aws: Aws.
            clock: Clock.
            project_root_path: Exact filesystem path for project root.
            region: Region.
        """

        self._aws = aws
        self._clock = clock
        self._project_root_path = project_root_path
        self._region = region

    def record(self) -> None:
        """Resolve current prices and write the approved architecture review."""

        instance_hour_price = self._price_usd_get(
            {
                "capacitystatus": "Used",
                "instanceType": "m7g.xlarge",
                "operatingSystem": "Linux",
                "preInstalledSw": "NA",
                "regionCode": self._region,
                "tenancy": "Shared",
            },
            unit="Hrs",
            usage_type="",
        )
        gp3_gib_month_price = self._price_usd_get(
            {
                "regionCode": self._region,
                "volumeApiName": "gp3",
            },
            unit="GB-Mo",
            usage_type="EBS:VolumeUsage.gp3",
        )
        snapshot_gib_month_price = self._price_usd_get(
            {
                "productFamily": "Storage Snapshot",
                "regionCode": self._region,
            },
            unit="GB-Mo",
            usage_type="EBS:SnapshotUsage",
        )
        active_hour_count_monthly = Decimal(80)
        gp3_gib_count_max = Decimal(260)
        snapshot_retention_count = Decimal(7)
        snapshot_source_volume_gib_count_max = Decimal(80)
        snapshot_stored_gib_count_max = snapshot_retention_count * snapshot_source_volume_gib_count_max
        usage_price_meter_spec_by_service_name_map = {
            "athena": {
                "data_scanned": (
                    "AmazonAthena",
                    {
                        "regionCode": self._region,
                        "usagetype": "USE1-DataScannedInTB",
                    },
                    "USE1-DataScannedInTB",
                ),
            },
            "data_transfer": {
                "internet_outbound": (
                    "AWSDataTransfer",
                    {
                        "fromRegionCode": self._region,
                        "toLocation": "External",
                        "transferType": "AWS Outbound",
                        "usagetype": "DataTransfer-Out-Bytes",
                    },
                    "DataTransfer-Out-Bytes",
                ),
            },
            "glue": {
                "catalog_request": (
                    "AWSGlue",
                    {
                        "regionCode": self._region,
                        "usagetype": "USE1-Catalog-Request",
                    },
                    "USE1-Catalog-Request",
                ),
                "catalog_storage": (
                    "AWSGlue",
                    {
                        "regionCode": self._region,
                        "usagetype": "USE1-Catalog-Storage",
                    },
                    "USE1-Catalog-Storage",
                ),
            },
            "kms": {
                "customer_managed_key": (
                    "awskms",
                    {
                        "regionCode": self._region,
                        "usagetype": "us-east-1-KMS-Keys",
                    },
                    "us-east-1-KMS-Keys",
                ),
                "request": (
                    "awskms",
                    {
                        "regionCode": self._region,
                        "usagetype": "us-east-1-KMS-Requests",
                    },
                    "us-east-1-KMS-Requests",
                ),
            },
            "s3": {
                "standard_storage": (
                    "AmazonS3",
                    {
                        "regionCode": self._region,
                        "storageClass": "General Purpose",
                        "usagetype": "TimedStorage-ByteHrs",
                        "volumeType": "Standard",
                    },
                    "TimedStorage-ByteHrs",
                ),
                "tier_1_request": (
                    "AmazonS3",
                    {
                        "group": "S3-API-Tier1",
                        "regionCode": self._region,
                        "usagetype": "Requests-Tier1",
                    },
                    "Requests-Tier1",
                ),
                "tier_2_request": (
                    "AmazonS3",
                    {
                        "group": "S3-API-Tier2",
                        "regionCode": self._region,
                        "usagetype": "Requests-Tier2",
                    },
                    "Requests-Tier2",
                ),
            },
        }
        usage_based_service_by_name_map: dict[str, dict[str, object]] = {}
        price_dimension_list_by_service_meter_map: dict[
            tuple[str, str],
            list[dict[str, str]],
        ] = {}
        for (
            service_name,
            price_meter_spec_by_name_map,
        ) in usage_price_meter_spec_by_service_name_map.items():
            price_meter_by_name_map = {}
            for meter_name, (
                service_code,
                filter_by_field_map,
                usage_type,
            ) in price_meter_spec_by_name_map.items():
                price_dimension_list = self.price_dimension_list_get(
                    service_code=service_code,
                    filter_by_field_map=filter_by_field_map,
                    usage_type=usage_type,
                )
                price_dimension_list_by_service_meter_map[(service_name, meter_name)] = price_dimension_list
                price_meter_by_name_map[meter_name] = {
                    "price_dimension_list": price_dimension_list,
                    "service_code": service_code,
                    "usage_type": usage_type,
                }
            usage_based_service_by_name_map[service_name] = {
                "architecture_delta_monthly_usd": "0.00",
                "assumption": (
                    "Existing approved usage quantity is unchanged; " "architecture delta quantity is zero."
                ),
                "price_meter_by_name_map": price_meter_by_name_map,
            }
        kms_key_price_dimension_list = price_dimension_list_by_service_meter_map[("kms", "customer_managed_key")]
        kms_key_price_set = {
            Decimal(price_dimension["price_per_unit_usd"])
            for price_dimension in kms_key_price_dimension_list
            if price_dimension["unit"] == "Keys"
        }
        if len(kms_key_price_set) != 1:
            raise DevelopmentEnvironmentError("AWS Pricing did not return one KMS key price")
        kms_key_monthly_price = next(iter(kms_key_price_set))
        kms_customer_managed_key_count = Decimal(1)
        estimated_compute_monthly = instance_hour_price * active_hour_count_monthly
        estimated_gp3_monthly_max = gp3_gib_month_price * gp3_gib_count_max
        estimated_snapshot_monthly_max = snapshot_gib_month_price * snapshot_stored_gib_count_max
        estimated_kms_key_monthly = kms_key_monthly_price * kms_customer_managed_key_count
        retained_rollback_monthly_delta_max = gp3_gib_month_price * Decimal(80)
        review_payload = {
            "architecture_delta_monthly_usd": {
                "bounded_retained_rollback_volume_max": str(
                    retained_rollback_monthly_delta_max.quantize(Decimal("0.01"))
                ),
                "total_max": str(retained_rollback_monthly_delta_max.quantize(Decimal("0.01"))),
            },
            "architecture_checkpoint": "approved-2026-07-28",
            "assumption": {
                "active_hour_count_monthly": int(active_hour_count_monthly),
                "gp3_gib_count_max": int(gp3_gib_count_max),
                "kms_customer_managed_key_count": int(kms_customer_managed_key_count),
                "snapshot_retention_count": int(snapshot_retention_count),
                "snapshot_source_volume_gib_count_max": int(snapshot_source_volume_gib_count_max),
                "snapshot_stored_gib_count_max": int(snapshot_stored_gib_count_max),
            },
            "estimated_monthly_usd": {
                "compute": str(estimated_compute_monthly.quantize(Decimal("0.01"))),
                "gp3_max": str(estimated_gp3_monthly_max.quantize(Decimal("0.01"))),
                "kms_customer_managed_key": str(estimated_kms_key_monthly.quantize(Decimal("0.01"))),
                "snapshot_max": str(estimated_snapshot_monthly_max.quantize(Decimal("0.01"))),
                "total_fixed_max": str(
                    (
                        estimated_compute_monthly
                        + estimated_gp3_monthly_max
                        + estimated_kms_key_monthly
                        + estimated_snapshot_monthly_max
                    ).quantize(Decimal("0.01"))
                ),
            },
            "price_usd": {
                "gp3_gib_month": str(gp3_gib_month_price),
                "kms_customer_managed_key_month": str(kms_key_monthly_price),
                "m7g_xlarge_hour": str(instance_hour_price),
                "snapshot_gib_month": str(snapshot_gib_month_price),
            },
            "t_calculate": self._clock.now().isoformat().replace("+00:00", "Z"),
            "usage_based_service_by_name_map": usage_based_service_by_name_map,
        }
        review_path = self._project_root_path / ".local" / "cost-review.json"
        review_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        review_path.write_text(
            json.dumps(review_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(review_path, 0o600)
        print(json.dumps(review_payload, indent=2, sort_keys=True))

    def price_dimension_list_get(
        self,
        *,
        service_code: str,
        filter_by_field_map: dict[str, str],
        usage_type: str,
    ) -> list[dict[str, str]]:
        """Return every exact current on-demand price tier for one AWS meter.

        Args:
            service_code: Service code.
            filter_by_field_map: Filter by field mapping.
            usage_type: Usage type.

        Returns:
            Every exact current on-demand price tier for the AWS meter.
        """

        aws_argument_list = [
            "pricing",
            "get-products",
            "--service-code",
            service_code,
            "--max-results",
            "100",
        ]
        if filter_by_field_map:
            aws_argument_list.append("--filters")
            for field, value in sorted(filter_by_field_map.items()):
                aws_argument_list.append(f"Type=TERM_MATCH,Field={field},Value={value}")
        payload = self._aws.json_get(aws_argument_list)
        price_list = payload.get("PriceList", [])
        if not isinstance(price_list, list):
            raise DevelopmentEnvironmentError("AWS Pricing response is malformed")
        price_dimension_set: set[tuple[str, str, str, str]] = set()
        for product_text in price_list:
            if not isinstance(product_text, str):
                raise DevelopmentEnvironmentError("AWS Pricing product is malformed")
            try:
                product_payload = json.loads(product_text)
            except json.JSONDecodeError as error:
                raise DevelopmentEnvironmentError("AWS Pricing product is invalid") from error
            if not isinstance(product_payload, dict):
                raise DevelopmentEnvironmentError("AWS Pricing product is malformed")
            product = product_payload.get("product", {})
            attribute_by_name_map = product.get("attributes", {}) if isinstance(product, dict) else {}
            if not isinstance(attribute_by_name_map, dict):
                continue
            if usage_type and attribute_by_name_map.get("usagetype") != usage_type:
                continue
            term_root = product_payload.get("terms", {})
            term_by_code_map = term_root.get("OnDemand", {}) if isinstance(term_root, dict) else {}
            if not isinstance(term_by_code_map, dict):
                continue
            for term_payload in term_by_code_map.values():
                if not isinstance(term_payload, dict):
                    continue
                dimension_by_code_map = term_payload.get("priceDimensions", {})
                if not isinstance(dimension_by_code_map, dict):
                    continue
                for dimension_payload in dimension_by_code_map.values():
                    if not isinstance(dimension_payload, dict):
                        continue
                    price_per_unit = dimension_payload.get("pricePerUnit", {})
                    price_text = price_per_unit.get("USD") if isinstance(price_per_unit, dict) else None
                    begin_range = dimension_payload.get("beginRange")
                    end_range = dimension_payload.get("endRange")
                    dimension_unit = dimension_payload.get("unit")
                    if not all(
                        isinstance(value, str)
                        for value in (
                            begin_range,
                            end_range,
                            price_text,
                            dimension_unit,
                        )
                    ):
                        continue
                    try:
                        Decimal(begin_range)
                        Decimal(price_text)
                    except (InvalidOperation, ValueError) as error:
                        raise DevelopmentEnvironmentError("AWS Pricing dimension is invalid") from error
                    price_dimension_set.add(
                        (
                            begin_range,
                            end_range,
                            price_text,
                            dimension_unit,
                        )
                    )
        if not price_dimension_set:
            raise DevelopmentEnvironmentError(
                f"AWS Pricing returned no price dimensions for {service_code} "
                f"usage type {usage_type or 'unspecified'}"
            )
        return [
            {
                "begin_range": begin_range,
                "end_range": end_range,
                "price_per_unit_usd": price_text,
                "unit": unit,
            }
            for begin_range, end_range, price_text, unit in sorted(
                price_dimension_set,
                key=lambda price_dimension: Decimal(price_dimension[0]),
            )
        ]

    def _price_usd_get(
        self,
        filter_by_field_map: dict[str, str],
        *,
        service_code: str = "AmazonEC2",
        unit: str,
        usage_type: str,
    ) -> Decimal:
        """Return one unambiguous price from the exact meter tiers.

        Args:
            filter_by_field_map: Filter by field mapping.
            service_code: Service code.
            unit: Unit.
            usage_type: Usage type.

        Returns:
            One unambiguous price from the exact meter tiers.
        """

        price_dimension_list = self.price_dimension_list_get(
            service_code=service_code,
            filter_by_field_map=filter_by_field_map,
            usage_type=usage_type,
        )
        price_set = {
            Decimal(price_dimension["price_per_unit_usd"])
            for price_dimension in price_dimension_list
            if price_dimension["unit"] == unit
        }
        if len(price_set) != 1:
            raise DevelopmentEnvironmentError(
                f"AWS Pricing returned {len(price_set)} distinct {unit} prices "
                f"for usage type {usage_type or 'instance'}"
            )
        return next(iter(price_set))
