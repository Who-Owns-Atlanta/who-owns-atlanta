-- Geometry-matching trigger for application.records
-- Two-stage: 1) Address_Point match  2) Tax_Parcel fallback
-- Adapted from nbh_accela/docker/init-db/04-add-address-match.sql

CREATE OR REPLACE FUNCTION application.update_record_geom() RETURNS TRIGGER AS $$
DECLARE
    -- Address vars
    addr_obj jsonb;
    a_num text;
    a_name text;
    a_suffix text;
    a_dir text;
    gis_suffix text;
    gis_name text;

    -- Parcel vars
    p_number text;

    -- Result vars
    found_geom geometry;
    match_count int;
BEGIN
    found_geom := NULL;

    -- =========================================================
    -- STRATEGY 1: Address Matching (Address_Point)
    -- =========================================================

    addr_obj := NEW.raw_data #> '{addresses, 0}';

    IF addr_obj IS NOT NULL THEN
        a_num := addr_obj ->> 'streetStart';
        a_name := addr_obj ->> 'streetName';
        a_suffix := addr_obj #>> '{streetSuffix, value}';
        a_dir := addr_obj #>> '{streetSuffixDirection, value}';

        -- Normalize inputs
        a_name := UPPER(TRIM(a_name));
        a_suffix := UPPER(TRIM(a_suffix));
        a_dir := UPPER(TRIM(a_dir));

        -- Map Suffix (Accela abbreviation -> GIS full word)
        SELECT gis_val INTO gis_suffix
        FROM application.suffix_map
        WHERE accela_val = a_suffix;

        IF gis_suffix IS NULL THEN
            gis_suffix := a_suffix;
        END IF;

        -- Map Ordinal Name (TENTH -> 10TH)
        SELECT num_val INTO gis_name
        FROM application.ordinal_map
        WHERE word_val = a_name;

        IF gis_name IS NULL THEN
            gis_name := a_name;
        END IF;

        IF a_num IS NOT NULL AND a_name IS NOT NULL THEN
            SELECT count(*), min(ap.geometry) INTO match_count, found_geom
            FROM gis."Address_Point" ap
            WHERE ap."ADDRNUM" = a_num
              AND (UPPER(ap."ADDR_SN") = gis_name OR UPPER(ap."ADDR_SN") = a_name)
              AND (
                  (gis_suffix IS NULL AND ap."ADDR_ST" IS NULL) OR
                  (UPPER(ap."ADDR_ST") = gis_suffix) OR
                  (UPPER(ap."ADDR_ST") = a_suffix)
              )
              AND (
                  (a_dir IS NULL AND ap."ADDR_SD" IS NULL) OR
                  (UPPER(ap."ADDR_SD") = a_dir)
              );

            IF match_count > 0 THEN
                NEW.geom := found_geom;
                RETURN NEW;
            END IF;
        END IF;
    END IF;

    -- =========================================================
    -- STRATEGY 2: Parcel Matching (Tax_Parcel)
    -- =========================================================

    -- Extract primary parcel number
    SELECT p->>'parcelNumber' INTO p_number
    FROM jsonb_array_elements(NEW.raw_data -> 'parcels') p
    WHERE p->>'isPrimary' = 'Y'
    LIMIT 1;

    IF p_number IS NULL THEN
        p_number := NEW.raw_data #>> '{parcels, 0, parcelNumber}';
    END IF;

    IF p_number IS NOT NULL THEN
        -- 1. Try exact match on LOWPARCELID
        SELECT count(*), min(tp.geometry) INTO match_count, found_geom
        FROM gis."Tax_Parcel" tp
        WHERE tp."LOWPARCELID" = p_number;

        IF match_count = 0 THEN
            -- 2. Try exact match on PARCELID
            SELECT count(*), min(tp.geometry) INTO match_count, found_geom
            FROM gis."Tax_Parcel" tp
            WHERE tp."PARCELID" = p_number;
        END IF;

        IF match_count = 0 THEN
            -- 3. Try prefix match on PARCELID (fallback)
            SELECT count(*), min(tp.geometry) INTO match_count, found_geom
            FROM gis."Tax_Parcel" tp
            WHERE tp."PARCELID" LIKE p_number || '%';
        END IF;

        IF found_geom IS NOT NULL THEN
            NEW.geom := found_geom;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create the trigger
DROP TRIGGER IF EXISTS trg_update_record_geom ON application.records;
CREATE TRIGGER trg_update_record_geom
    BEFORE INSERT OR UPDATE OF raw_data ON application.records
    FOR EACH ROW
    EXECUTE FUNCTION application.update_record_geom();
