import json
import logging
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import Select, and_, func
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.utils import is_bool_like
from app.database import get_db_session
from app.models import (
    BooleanPropertyValue,
    DatasetEntity,
    Listing,
    Property,
    PropertyType,
    StringPropertyValue,
)
from app.schemas.request import (
    Entity,
    ListingGetRequest,
    UpsertListing,
    UpsertListingsRequest,
)
from app.schemas.response import ListingGet, ListingsGetResponse, UpsertListingsResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/listings", tags=["listings"])


class PropertyValueLike(BaseModel):
    name: str
    type: str
    value: str


@router.get("/", response_model=ListingsGetResponse)
def get_listings(filters: Annotated[ListingGetRequest, Query()]):
    """Get all listings with optional filters."""
    logger.info(f"GET /listings/ - Filters: {filters}")
    PAGE_SIZE = 100

    with get_db_session() as session:
        statement = (
            select(
                Listing,
                func.coalesce(
                    func.json_agg(
                        func.json_build_object(
                            "name", DatasetEntity.name, "data", DatasetEntity.data
                        )
                    ),
                    func.json_build_array(),
                ).label("entities"),
            )
            .options(
                selectinload(Listing.string_property_values).selectinload(
                    StringPropertyValue.property
                ),
                selectinload(Listing.boolean_property_values).selectinload(
                    BooleanPropertyValue.property
                ),
            )
            .join(
                DatasetEntity, Listing.dataset_entity_ids.any(DatasetEntity.entity_id)
            )
            .filter(DatasetEntity.name.is_not(None))
            .group_by(Listing.listing_id)
        )

        if filters.dataset_entities:
            statement = statement.where(
                and_(
                    func.cardinality(Listing.dataset_entity_ids) > 0,
                    DatasetEntity.data.op("@>")(json.loads(filters.dataset_entities)),
                )
            )

        has_property_filters, listing_ids = _get_property_filtered_ids(
            filters.properties, session, filters.listing_id
        )
        if has_property_filters:
            # If property filters found no match, return empty response
            if not listing_ids:
                return ListingsGetResponse(listings=[], total=0)

            statement = statement.where(Listing.listing_id.in_(listing_ids))

        statement = _add_filters(statement, filters)

        total_count = _get_count(session, filters)

        if filters.page:
            statement = statement.offset((filters.page - 1) * PAGE_SIZE)

        statement = statement.order_by(Listing.listing_id).limit(PAGE_SIZE)
        results = session.exec(statement).all()

        formatted_results = _get_formatted_results(results)

        return ListingsGetResponse(
            listings=formatted_results,
            total=total_count,
        )


def _get_count(session: Session, filters: ListingGetRequest) -> int:
    # Create a separate count query without json_agg to avoid duplicates
    count_statement = (
        select(func.count(Listing.listing_id.distinct()))
        .join(DatasetEntity, Listing.dataset_entity_ids.any(DatasetEntity.entity_id))
        .filter(DatasetEntity.name.is_not(None))
    )

    if filters.dataset_entities:
        count_statement = count_statement.where(
            and_(
                func.cardinality(Listing.dataset_entity_ids) > 0,
                DatasetEntity.data.op("@>")(json.loads(filters.dataset_entities)),
            )
        )

    has_property_filters, listing_ids = _get_property_filtered_ids(
        filters.properties, session
    )
    if has_property_filters:
        # The no-match case should ideally not reach here
        count_statement = count_statement.where(Listing.listing_id.in_(listing_ids))

    count_statement = _add_filters(count_statement, filters)

    result = session.exec(count_statement)
    total_count = result.one() if result is not None else 0

    return total_count


def _add_property_filters(statement: Select, property_filters: list) -> Select:
    """Add property filters to the query statement."""
    if property_filters:
        combined_filter = property_filters[0]
        for filter_condition in property_filters[1:]:
            combined_filter = combined_filter & filter_condition
        statement = statement.where(combined_filter)
    return statement


def _get_property_filtered_ids(
    properties: str, session: Session, listing_id_filter: str | None = None
) -> tuple[bool, list[str]]:
    """
    Returns a tuple of (has_property_filters, listing_ids)
    """

    def _get_property_type(property_id: int) -> str:
        return session.exec(
            select(Property.type).where(Property.property_id == property_id)
        ).one()

    properties = json.loads(properties) if properties else {}

    if not properties:
        return False, []

    # Create a {"string": [...], "boolean": [...]} map
    type_filter_map = defaultdict(list)
    for property_id, expected_value in properties.items():
        property_id = int(property_id)
        property_type = _get_property_type(property_id)
        type_filter_map[property_type].append(
            {
                "property_id": property_id,
                "value": expected_value,
            }
        )

    listing_ids = []
    for property_type, filters in type_filter_map.items():
        listing_ids.extend(
            _get_listing_ids_for_property_type(
                property_type, filters, session, listing_id_filter
            )
        )

    return True, listing_ids


def _get_listing_ids_for_property_type(
    property_type: str,
    filters: list[dict],
    session: Session,
    listing_id_filter: str | None = None,
) -> list[str]:
    if property_type == PropertyType.BOOLEAN:
        prop_filter_query = select(BooleanPropertyValue.listing_id)
        for filter in filters:
            bool_value = (
                TypeAdapter(bool).validate_python(filter["value"])
                if is_bool_like(filter["value"])
                else filter["value"]
            )
            prop_filter_query = prop_filter_query.where(
                BooleanPropertyValue.property_id == filter["property_id"],
                BooleanPropertyValue.value == bool_value,
            )

        if listing_id_filter:
            prop_filter_query = prop_filter_query.where(
                BooleanPropertyValue.listing_id == listing_id_filter
            )

        prop_filter_query = prop_filter_query.group_by(BooleanPropertyValue.listing_id)
        return session.exec(prop_filter_query).all()
    elif property_type == PropertyType.STRING:
        prop_filter_query = select(StringPropertyValue.listing_id)
        for filter in filters:
            prop_filter_query = prop_filter_query.where(
                StringPropertyValue.property_id == filter["property_id"],
                StringPropertyValue.value == filter["value"],
            )

        if listing_id_filter:
            prop_filter_query = prop_filter_query.where(
                StringPropertyValue.listing_id == listing_id_filter
            )

        prop_filter_query = prop_filter_query.group_by(StringPropertyValue.listing_id)
        return session.exec(prop_filter_query).all()
    else:
        raise ValueError(f"Invalid property type: {property_type}")


def _add_filters(statement: Select, filters: ListingGetRequest):
    """Add filters to the query statement."""
    if filters.listing_id:
        statement = statement.where(Listing.listing_id == filters.listing_id)

    if filters.scan_date_from:
        statement = statement.where(Listing.scan_date >= filters.scan_date_from)

    if filters.scan_date_to:
        statement = statement.where(Listing.scan_date <= filters.scan_date_to)

    if filters.is_active is not None:
        statement = statement.where(Listing.is_active == filters.is_active)

    if filters.image_hashes:
        statement = statement.where(Listing.image_hashes.op("&&")(filters.image_hashes))

    return statement


def _get_formatted_results(
    results: list[tuple[Listing, list[dict]]],
) -> list[ListingGet]:
    formatted_results = []
    for result in results:
        listing, entities = result
        str_properties = listing.string_property_values
        bool_properties = listing.boolean_property_values

        properties = []
        for property in str_properties + bool_properties:
            properties.append(
                {
                    "name": property.property.name,
                    "type": "str"
                    if property.property.type == PropertyType.STRING
                    else "bool",
                    "value": property.value,
                }
            )

        formatted_results.append(
            ListingGet(
                listing_id=listing.listing_id,
                scan_date=listing.scan_date.isoformat(sep=" ")
                if listing.scan_date
                else "",
                is_active=listing.is_active,
                image_hashes=listing.image_hashes,
                properties=properties,
                entities=entities,
            )
        )

    return formatted_results


@router.put("/", response_model=UpsertListingsResponse)
def upsert_listings(listings_data: UpsertListingsRequest):
    """
    Insert or update multiple listings with their properties and entities.
    `UpsertListingsRequest` is the source of truth.
    """
    logger.info(f"PUT /listings/ - Upserting {len(listings_data.listings)} listings")

    with get_db_session() as session:
        listings = listings_data.listings
        current_listing_index = ""

        try:
            for index, listing_data in enumerate(listings):
                current_listing_index = listing_data.listing_id

                listing_obj = _upsert_listing(listing_data, session)

                _upsert_properties(
                    properties=listing_data.properties,
                    session=session,
                    listing_id=listing_data.listing_id,
                )

                entity_ids = _upsert_entities(
                    entities=listing_data.entities,
                    session=session,
                    listing_id=listing_data.listing_id,
                )

                # Update listing with entity IDs
                listing_obj.dataset_entity_ids = entity_ids

            return UpsertListingsResponse(status="success", error=None)
        except Exception as e:
            session.rollback()
            return UpsertListingsResponse(
                status="failed",
                error={"listing_id": current_listing_index, "error": str(e)},
            )


def _upsert_listing(listing_data: UpsertListing, session: Session) -> Listing:
    existing_listing = session.exec(
        select(Listing).where(Listing.listing_id == listing_data.listing_id)
    ).first()

    if existing_listing:
        # Update existing listing
        existing_listing.scan_date = listing_data.scan_date
        existing_listing.is_active = listing_data.is_active
        existing_listing.image_hashes = listing_data.image_hashes
        existing_listing.dataset_entity_ids = []  # Will be populated from entities
        session.add(existing_listing)

        listing_obj = existing_listing
    else:
        # Create new listing
        new_listing = Listing(
            listing_id=listing_data.listing_id,
            scan_date=listing_data.scan_date,
            is_active=listing_data.is_active,
            image_hashes=listing_data.image_hashes,
            dataset_entity_ids=[],
        )
        session.add(new_listing)
        listing_obj = new_listing

    return listing_obj


def _upsert_properties(properties: list[Property], session: Session, listing_id: str):
    """Upsert properties for a listing."""
    property_table_map = {
        "str": StringPropertyValue,
        "string": StringPropertyValue,
        "bool": BooleanPropertyValue,
        "boolean": BooleanPropertyValue,
    }

    formatter = {
        StringPropertyValue: lambda x: x.value,
        BooleanPropertyValue: lambda x: x.value.lower() == "true",
    }

    for property_data in properties:
        # Find or create Property record
        property_type = property_data.type.lower()
        property_record = session.exec(
            select(Property).where(Property.name == property_data.name)
        ).first()

        if not property_record:
            is_str_property = property_type in ["str", "string"]
            property_record = Property(
                name=property_data.name,
                type=PropertyType.STRING if is_str_property else PropertyType.BOOLEAN,
            )
            session.add(property_record)
            session.flush()  # Get the property_id

        # Property Table. Eg. StringPropertyValue / BooleanPropertyValue
        VALUE_TABLE: type[PropertyValueLike] = property_table_map[property_type]

        existing_value = session.exec(
            select(VALUE_TABLE).where(
                VALUE_TABLE.listing_id == listing_id,
                VALUE_TABLE.property_id == property_record.property_id,
            )
        ).first()

        # Format the value to the correct type
        value = formatter[VALUE_TABLE](property_data)

        # Upsert the value
        if existing_value:
            existing_value.value = value
            session.add(existing_value)
        else:
            property_value = VALUE_TABLE(
                listing_id=listing_id,
                property_id=property_record.property_id,
                value=value,
            )
            session.add(property_value)


def _upsert_entities(
    entities: list[Entity], session: Session, listing_id: str
) -> list[int]:
    entity_ids = []

    for entity_data in entities:
        # Find or create DatasetEntity
        entity_record = session.exec(
            select(DatasetEntity).where(DatasetEntity.name == entity_data.name)
        ).first()

        if not entity_record:
            entity_record = DatasetEntity(name=entity_data.name, data=entity_data.data)
            session.add(entity_record)
            session.flush()  # Get the entity_id
        else:
            # Update existing entity data
            entity_record.data = entity_data.data
            session.add(entity_record)

        entity_ids.append(entity_record.entity_id)

    return entity_ids
