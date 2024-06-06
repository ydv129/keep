# TODO: this whole file needs to get refactored
# mainly: pusher stuff, enrichment stuff and async stuff
import json
import logging
import os

from arq import ArqRedis
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pusher import Pusher

from keep.api.arq_worker import get_pool
from keep.api.bl.enrichments import EnrichmentsBl
from keep.api.core.config import config
from keep.api.core.db import (
    get_alerts_by_fingerprint,
    get_all_presets,
    get_enrichment,
    get_last_alerts,
)
from keep.api.core.dependencies import (
    AuthenticatedEntity,
    AuthVerifier,
    get_pusher_client,
)
from keep.api.core.elastic import ElasticClient
from keep.api.models.alert import (
    AlertDto,
    AlertStatus,
    DeleteRequestBody,
    EnrichAlertRequestBody,
)
from keep.api.models.db.preset import PresetDto
from keep.api.models.search_alert import SearchAlertsRequest
from keep.api.tasks.process_event_task import process_event
from keep.api.utils.email_utils import EmailTemplates, send_email
from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
from keep.contextmanager.contextmanager import ContextManager
from keep.providers.providers_factory import ProvidersFactory
from keep.rulesengine.rulesengine import RulesEngine
from keep.searchengine.searchengine import SearchEngine

router = APIRouter()
logger = logging.getLogger(__name__)

elastic_client = ElasticClient()

REDIS = os.environ.get("REDIS", "false") == "true"


def pull_alerts_from_providers(
    tenant_id: str, pusher_client: Pusher | None, sync: bool = False
) -> list[AlertDto]:
    """
    Pulls alerts from the installed providers.
    tb: THIS FUNCTION NEEDS TO BE REFACTORED!

    Args:
        tenant_id (str): The tenant id.
        pusher_client (Pusher | None): The pusher client.
        sync (bool, optional): Whether the process is sync or not. Defaults to False.

    Raises:
        HTTPException: If the pusher client is None and the process is not sync.

    Returns:
        list[AlertDto]: The pulled alerts.
    """
    if pusher_client is None and sync is False:
        raise HTTPException(500, "Cannot pull alerts async when pusher is disabled.")

    context_manager = ContextManager(
        tenant_id=tenant_id,
        workflow_id=None,
    )

    logger.info(
        f"{'Asynchronously' if sync is False else 'Synchronously'} pulling alerts from installed providers"
    )

    sync_alerts = []  # if we're running in sync mode
    for provider in ProvidersFactory.get_installed_providers(tenant_id=tenant_id):
        provider_class = ProvidersFactory.get_provider(
            context_manager=context_manager,
            provider_id=provider.id,
            provider_type=provider.type,
            provider_config=provider.details,
        )
        try:
            logger.info(
                f"Pulling alerts from provider {provider.type} ({provider.id})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                },
            )
            sorted_provider_alerts_by_fingerprint = (
                provider_class.get_alerts_by_fingerprint(tenant_id=tenant_id)
            )
            logger.info(
                f"Pulled alerts from provider {provider.type} ({provider.id})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                    "number_of_fingerprints": len(
                        sorted_provider_alerts_by_fingerprint.keys()
                    ),
                },
            )

            if sorted_provider_alerts_by_fingerprint:
                last_alerts = [
                    alerts[0]
                    for alerts in sorted_provider_alerts_by_fingerprint.values()
                ]
                if sync:
                    sync_alerts.extend(last_alerts)
                    logger.info(
                        f"Pulled alerts from provider {provider.type} ({provider.id}) (alerts: {len(sorted_provider_alerts_by_fingerprint)})",
                        extra={
                            "provider_type": provider.type,
                            "provider_id": provider.id,
                            "tenant_id": tenant_id,
                        },
                    )
                    continue

                logger.info("Batch sending pulled alerts via pusher")
                batch_send = []
                previous_compressed_batch = ""
                new_compressed_batch = ""
                number_of_alerts_in_batch = 0
                # tb: this might be too slow in the future and we might need to refactor
                for alert in last_alerts:
                    alert_dict = alert.dict()
                    batch_send.append(alert_dict)
                    new_compressed_batch = json.dumps(batch_send)
                    if len(new_compressed_batch) <= 10240:
                        number_of_alerts_in_batch += 1
                        previous_compressed_batch = new_compressed_batch
                    elif pusher_client:
                        pusher_client.trigger(
                            f"private-{tenant_id}",
                            "async-alerts",
                            previous_compressed_batch,
                        )
                        batch_send = [alert_dict]
                        new_compressed_batch = ""
                        number_of_alerts_in_batch = 1

                # this means we didn't get to this ^ else statement and loop ended
                #   so we need to send the rest of the alerts
                if (
                    new_compressed_batch
                    and len(new_compressed_batch) < 10240
                    and pusher_client
                ):
                    pusher_client.trigger(
                        f"private-{tenant_id}",
                        "async-alerts",
                        new_compressed_batch,
                    )
                logger.info("Sent batch of pulled alerts via pusher")
                # Also update the presets
                try:
                    presets = get_all_presets(tenant_id)
                    presets_do_update = []
                    for preset in presets:
                        # filter the alerts based on the search query
                        preset_dto = PresetDto(**preset.dict())
                        filtered_alerts = RulesEngine.filter_alerts(
                            last_alerts, preset_dto.cel_query
                        )
                        # if not related alerts, no need to update
                        if not filtered_alerts:
                            continue
                        presets_do_update.append(preset_dto)
                        preset_dto.alerts_count = len(filtered_alerts)
                        # update noisy
                        if preset.is_noisy:
                            firing_filtered_alerts = list(
                                filter(
                                    lambda alert: alert.status
                                    == AlertStatus.FIRING.value,
                                    filtered_alerts,
                                )
                            )
                            # if there are firing alerts, then do noise
                            if firing_filtered_alerts:
                                logger.info("Noisy preset is noisy")
                                preset_dto.should_do_noise_now = True
                        # else if at least one of the alerts has .isNoisy
                        elif any(
                            alert.isNoisy and alert.status == AlertStatus.FIRING.value
                            for alert in filtered_alerts
                            if hasattr(alert, "isNoisy")
                        ):
                            logger.info("Noisy preset is noisy")
                            preset_dto.should_do_noise_now = True
                    # send with pusher
                    if pusher_client:
                        try:
                            pusher_client.trigger(
                                f"private-{tenant_id}",
                                "async-presets",
                                json.dumps(
                                    [p.dict() for p in presets_do_update], default=str
                                ),
                            )
                        except Exception:
                            logger.exception("Failed to send presets via pusher")
                except Exception:
                    logger.exception(
                        "Failed to send presets via pusher",
                        extra={
                            "provider_type": provider.type,
                            "provider_id": provider.id,
                            "tenant_id": tenant_id,
                        },
                    )
            logger.info(
                f"Pulled alerts from provider {provider.type} ({provider.id}) (alerts: {len(sorted_provider_alerts_by_fingerprint)})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                },
            )
        except Exception as e:
            logger.warning(
                f"Could not fetch alerts from provider due to {e}",
                extra={
                    "provider_id": provider.id,
                    "provider_type": provider.type,
                    "tenant_id": tenant_id,
                },
            )
            pass
    if sync is False and pusher_client:
        pusher_client.trigger(f"private-{tenant_id}", "async-done", {})
    logger.info("Fetched alerts from installed providers")
    return sync_alerts


@router.get(
    "",
    description="Get last alerts occurrence",
)
def get_all_alerts(
    background_tasks: BackgroundTasks,
    sync: bool = False,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
    pusher_client: Pusher | None = Depends(get_pusher_client),
) -> list[AlertDto]:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Fetching alerts from DB",
        extra={
            "tenant_id": tenant_id,
        },
    )
    db_alerts = get_last_alerts(tenant_id=tenant_id)
    enriched_alerts_dto = convert_db_alerts_to_dto_alerts(db_alerts)
    logger.info(
        "Fetched alerts from DB",
        extra={
            "tenant_id": tenant_id,
        },
    )

    if sync:
        enriched_alerts_dto.extend(
            pull_alerts_from_providers(tenant_id, pusher_client, sync=True)
        )
    else:
        logger.info("Adding task to async fetch alerts from providers")
        background_tasks.add_task(pull_alerts_from_providers, tenant_id, pusher_client)
        logger.info("Added task to async fetch alerts from providers")

    return enriched_alerts_dto


@router.get("/{fingerprint}/history", description="Get alert history")
def get_alert_history(
    fingerprint: str,
    provider_id: str | None = None,
    provider_type: str | None = None,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
) -> list[AlertDto]:
    logger.info(
        "Fetching alert history",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": authenticated_entity.tenant_id,
        },
    )
    db_alerts = get_alerts_by_fingerprint(
        tenant_id=authenticated_entity.tenant_id, fingerprint=fingerprint, limit=1000
    )
    enriched_alerts_dto = convert_db_alerts_to_dto_alerts(db_alerts)

    if provider_id is not None and provider_type is not None:
        try:
            installed_provider = ProvidersFactory.get_installed_provider(
                tenant_id=authenticated_entity.tenant_id,
                provider_id=provider_id,
                provider_type=provider_type,
            )
            pulled_alerts_history = installed_provider.get_alerts_by_fingerprint(
                tenant_id=authenticated_entity.tenant_id
            ).get(fingerprint, [])
            enriched_alerts_dto.extend(pulled_alerts_history)
        except Exception:
            logger.warning(
                "Failed to pull alerts history from installed provider",
                extra={
                    "provider_id": provider_id,
                    "provider_type": provider_type,
                    "tenant_id": authenticated_entity.tenant_id,
                },
            )

    logger.info(
        "Fetched alert history",
        extra={
            "tenant_id": authenticated_entity.tenant_id,
            "fingerprint": fingerprint,
        },
    )
    return enriched_alerts_dto


@router.delete("", description="Delete alert by finerprint and last received time")
def delete_alert(
    delete_alert: DeleteRequestBody,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["delete:alert"])),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    user_email = authenticated_entity.email

    logger.info(
        "Deleting alert",
        extra={
            "fingerprint": delete_alert.fingerprint,
            "restore": delete_alert.restore,
            "lastReceived": delete_alert.lastReceived,
            "tenant_id": tenant_id,
        },
    )

    deleted_last_received = []  # the last received(s) that are deleted
    assignees_last_receievd = {}  # the last received(s) that are assigned to someone

    # If we enriched before, get the enrichment
    enrichment = get_enrichment(tenant_id, delete_alert.fingerprint)
    if enrichment:
        deleted_last_received = enrichment.enrichments.get("deletedAt", [])
        assignees_last_receievd = enrichment.enrichments.get("assignees", {})

    if (
        delete_alert.restore is True
        and delete_alert.lastReceived in deleted_last_received
    ):
        # Restore deleted alert
        deleted_last_received.remove(delete_alert.lastReceived)
    elif (
        delete_alert.restore is False
        and delete_alert.lastReceived not in deleted_last_received
    ):
        # Delete the alert if it's not already deleted (wtf basically, shouldn't happen)
        deleted_last_received.append(delete_alert.lastReceived)

    if delete_alert.lastReceived not in assignees_last_receievd:
        # auto-assign the deleting user to the alert
        assignees_last_receievd[delete_alert.lastReceived] = user_email

    # overwrite the enrichment
    enrichment_bl = EnrichmentsBl(tenant_id)
    enrichment_bl.enrich_alert(
        fingerprint=delete_alert.fingerprint,
        enrichments={
            "deletedAt": deleted_last_received,
            "assignees": assignees_last_receievd,
        },
    )

    logger.info(
        "Deleted alert successfully",
        extra={
            "tenant_id": tenant_id,
            "restore": delete_alert.restore,
            "fingerprint": delete_alert.fingerprint,
        },
    )
    return {"status": "ok"}


@router.post(
    "/{fingerprint}/assign/{last_received}", description="Assign alert to user"
)
def assign_alert(
    fingerprint: str,
    last_received: str,
    unassign: bool = False,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    user_email = authenticated_entity.email
    logger.info(
        "Assigning alert",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": tenant_id,
        },
    )

    assignees_last_receievd = {}  # the last received(s) that are assigned to someone
    enrichment = get_enrichment(tenant_id, fingerprint)
    if enrichment:
        assignees_last_receievd = enrichment.enrichments.get("assignees", {})

    if unassign:
        assignees_last_receievd.pop(last_received, None)
    else:
        assignees_last_receievd[last_received] = user_email

    enrichment_bl = EnrichmentsBl(tenant_id)
    enrichment_bl.enrich_alert(
        fingerprint=fingerprint,
        enrichments={"assignees": assignees_last_receievd},
    )

    try:
        if not unassign:  # if we're assigning the alert to someone, send email
            logger.info("Sending assign alert email to user")
            # TODO: this should be changed to dynamic url but we don't know what's the frontend URL
            keep_platform_url = config(
                "KEEP_PLATFORM_URL", default="https://platform.keephq.dev"
            )
            url = f"{keep_platform_url}/alerts?fingerprint={fingerprint}"
            send_email(
                to_email=user_email,
                template_id=EmailTemplates.ALERT_ASSIGNED_TO_USER,
                url=url,
            )
            logger.info("Sent assign alert email to user")
    except Exception as e:
        logger.exception(
            "Failed to send email to user",
            extra={
                "error": str(e),
                "tenant_id": tenant_id,
                "user_email": user_email,
            },
        )

    logger.info(
        "Assigned alert successfully",
        extra={
            "tenant_id": tenant_id,
            "fingerprint": fingerprint,
        },
    )
    return {"status": "ok"}


@router.post(
    "/event",
    description="Receive a generic alert event",
    response_model=AlertDto | list[AlertDto],
    status_code=202,
)
async def receive_generic_event(
    event: AlertDto | list[AlertDto] | dict,
    bg_tasks: BackgroundTasks,
    request: Request,
    fingerprint: str | None = None,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
):
    """
    A generic webhook endpoint that can be used by any provider to send alerts to Keep.

    Args:
        alert (AlertDto | list[AlertDto]): The alert(s) to be sent to Keep.
        bg_tasks (BackgroundTasks): Background tasks handler.
        tenant_id (str, optional): Defaults to Depends(verify_api_key).
        session (Session, optional): Defaults to Depends(get_session).
    """
    if REDIS:
        redis: ArqRedis = await get_pool()
        await redis.enqueue_job(
            "process_event",
            authenticated_entity.tenant_id,
            None,
            None,
            fingerprint,
            authenticated_entity.api_key_name,
            request.state.trace_id,
            event,
        )
    else:
        bg_tasks.add_task(
            process_event,
            {},
            authenticated_entity.tenant_id,
            None,
            None,
            fingerprint,
            authenticated_entity.api_key_name,
            request.state.trace_id,
            event,
        )
    return Response(status_code=202)


@router.post(
    "/event/{provider_type}",
    description="Receive an alert event from a provider",
    status_code=202,
)
async def receive_event(
    provider_type: str,
    event: dict | bytes,
    bg_tasks: BackgroundTasks,
    request: Request,
    provider_id: str | None = None,
    fingerprint: str | None = None,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
) -> dict[str, str]:
    trace_id = request.state.trace_id
    provider_class = ProvidersFactory.get_provider_class(provider_type)
    # Parse the raw body
    event = provider_class.parse_event_raw_body(event)

    if REDIS:
        redis: ArqRedis = await get_pool()
        await redis.enqueue_job(
            "process_event",
            authenticated_entity.tenant_id,
            provider_type,
            provider_id,
            fingerprint,
            authenticated_entity.api_key_name,
            trace_id,
            event,
        )
    else:
        bg_tasks.add_task(
            process_event,
            {},
            provider_type,
            authenticated_entity.tenant_id,
            provider_id,
            fingerprint,
            authenticated_entity.api_key_name,
            trace_id,
            event,
        )
    return Response(status_code=202)


@router.get(
    "/{fingerprint}",
    description="Get alert by fingerprint",
)
def get_alert(
    fingerprint: str,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
) -> AlertDto:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Fetching alert",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": tenant_id,
        },
    )
    # TODO: once pulled alerts will be in the db too, this should be changed
    all_alerts = get_all_alerts(
        background_tasks=None, authenticated_entity=authenticated_entity, sync=True
    )
    alert = list(filter(lambda alert: alert.fingerprint == fingerprint, all_alerts))
    if alert:
        return alert[0]
    else:
        raise HTTPException(status_code=404, detail="Alert not found")


@router.post(
    "/enrich",
    description="Enrich an alert",
)
def enrich_alert(
    enrich_data: EnrichAlertRequestBody,
    background_tasks: BackgroundTasks,
    pusher_client: Pusher = Depends(get_pusher_client),
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["write:alert"])),
) -> dict[str, str]:
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        "Enriching alert",
        extra={
            "fingerprint": enrich_data.fingerprint,
            "tenant_id": tenant_id,
        },
    )

    try:
        enrichement_bl = EnrichmentsBl(tenant_id)
        enrichement_bl.enrich_alert(
            fingerprint=enrich_data.fingerprint,
            enrichments=enrich_data.enrichments,
        )
        # get the alert with the new enrichment
        alert = get_alerts_by_fingerprint(
            authenticated_entity.tenant_id, enrich_data.fingerprint, limit=1
        )
        if not alert:
            logger.warning(
                "Alert not found", extra={"fingerprint": enrich_data.fingerprint}
            )
            return {"status": "failed"}

        enriched_alerts_dto = convert_db_alerts_to_dto_alerts(alert)
        # push the enriched alert to the elasticsearch
        try:
            logger.info("Pushing enriched alert to elasticsearch")
            elastic_client.index_alert(
                tenant_id=tenant_id,
                alert=enriched_alerts_dto[0],
            )
            logger.info("Pushed enriched alert to elasticsearch")
        except Exception:
            logger.exception("Failed to push alert to elasticsearch")
            pass
        # use pusher to push the enriched alert to the client
        if pusher_client:
            logger.info("Pushing enriched alert to the client")
            try:
                pusher_client.trigger(
                    f"private-{tenant_id}",
                    "async-alerts",
                    json.dumps([enriched_alerts_dto[0].dict()]),
                )
                logger.info("Pushed enriched alert to the client")
            except Exception:
                logger.exception("Failed to push alert to the client")
                pass
        logger.info(
            "Alert enriched successfully",
            extra={"fingerprint": enrich_data.fingerprint, "tenant_id": tenant_id},
        )
        return {"status": "ok"}

    except Exception as e:
        logger.exception("Failed to enrich alert", extra={"error": str(e)})
        return {"status": "failed"}


@router.post(
    "/search",
    description="Search alerts",
)
async def search_alerts(
    search_request: SearchAlertsRequest,
    authenticated_entity: AuthenticatedEntity = Depends(AuthVerifier(["read:alert"])),
) -> list[AlertDto]:
    tenant_id = authenticated_entity.tenant_id
    try:
        logger.info(
            "Searching alerts",
            extra={"tenant_id": tenant_id},
        )
        search_engine = SearchEngine(tenant_id)
        filtered_alerts = search_engine.search_alerts(tenant_id, search_request.query)
        logger.info(
            "Searched alerts",
            extra={"tenant_id": tenant_id},
        )
        return filtered_alerts
    except Exception as e:
        logger.exception("Failed to search alerts", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to search alerts")
