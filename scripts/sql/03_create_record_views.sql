-- Post-processing views for Accela records
-- Links records to ownership data via Tax_Parcel parcel numbers

-- View: Records with parcel details from city Tax_Parcel
-- Bridges Accela parcelNumber -> Tax_Parcel LOWPARCELID -> county parcel data
CREATE OR REPLACE VIEW application.view_records_with_parcels AS
SELECT
    r.id AS record_db_id,
    r.accela_id,
    r.permit_number,
    r.description,
    r.status,
    r.opened_date,
    r.last_action_date,
    r.raw_data ->> 'closedDate' AS closed_date,
    r.geom,
    -- Parcel info from raw_data
    r.raw_data #>> '{parcels, 0, parcelNumber}' AS parcel_number,
    -- Tax_Parcel bridge data
    tp."PARCELID" AS county_parcel_id,
    tp."OWNERNME1" AS tax_owner,
    tp."CLASSCD" AS property_class,
    tp."SITEADDRESS" AS site_address,
    tp."NEIGHBORHOOD" AS neighborhood,
    tp."COUNCIL" AS council_district,
    tp."NPU" AS npu
FROM application.records r
LEFT JOIN gis."Tax_Parcel" tp
    ON r.raw_data #>> '{parcels, 0, parcelNumber}' = tp."LOWPARCELID";

-- View: Records linked to Fulton County ownership parcels
-- Uses Tax_Parcel as bridge: Accela parcelNumber -> LOWPARCELID -> PARCELID -> fulton_parcels.parcelid
CREATE OR REPLACE VIEW application.view_records_fulton AS
SELECT
    r.id AS record_db_id,
    r.permit_number,
    r.description,
    r.status,
    r.opened_date,
    r.last_action_date,
    r.raw_data ->> 'closedDate' AS closed_date,
    tp."PARCELID" AS county_parcel_id,
    fp.owner,
    fp.is_corporate,
    fp.is_institutional,
    fp.parcelid AS fulton_parcel_id
FROM application.records r
JOIN gis."Tax_Parcel" tp
    ON r.raw_data #>> '{parcels, 0, parcelNumber}' = tp."LOWPARCELID"
JOIN fulton_parcels fp
    ON tp."PARCELID" = fp.parcelid;

-- Summary view: complaint counts per parcel
CREATE OR REPLACE VIEW application.view_complaint_counts AS
SELECT
    r.raw_data #>> '{parcels, 0, parcelNumber}' AS parcel_number,
    tp."PARCELID" AS county_parcel_id,
    tp."SITEADDRESS" AS site_address,
    tp."OWNERNME1" AS tax_owner,
    count(*) AS complaint_count,
    count(*) FILTER (WHERE r.status = 'Open') AS open_complaints,
    count(*) FILTER (WHERE r.status = 'Closed') AS closed_complaints,
    min(r.opened_date) AS first_complaint,
    max(r.opened_date) AS last_complaint
FROM application.records r
LEFT JOIN gis."Tax_Parcel" tp
    ON r.raw_data #>> '{parcels, 0, parcelNumber}' = tp."LOWPARCELID"
WHERE r.raw_data #>> '{parcels, 0, parcelNumber}' IS NOT NULL
GROUP BY 1, 2, 3, 4;
