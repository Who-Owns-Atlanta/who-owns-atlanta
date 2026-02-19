-- Application schema for Accela permit records
-- Adapted from nbh_accela/docker/init-db/

-- 1. Create schemas
CREATE SCHEMA IF NOT EXISTS application;
CREATE SCHEMA IF NOT EXISTS gis;

-- 2. Helper function to recursively extract ALL text from a JSON object (for full-text search)
CREATE OR REPLACE FUNCTION application.extract_all_text(data jsonb) RETURNS text AS $$
DECLARE
    key text;
    value jsonb;
    text_output text := '';
BEGIN
    CASE jsonb_typeof(data)
        WHEN 'object' THEN
            FOR key, value IN SELECT * FROM jsonb_each(data) LOOP
                text_output := text_output || ' ' || application.extract_all_text(value);
            END LOOP;
        WHEN 'array' THEN
            FOR value IN SELECT * FROM jsonb_array_elements(data) LOOP
                text_output := text_output || ' ' || application.extract_all_text(value);
            END LOOP;
        WHEN 'string' THEN
            text_output := data #>> '{}';
        WHEN 'number' THEN
            text_output := data #>> '{}';
        WHEN 'boolean' THEN
            text_output := data #>> '{}';
        ELSE
            text_output := '';
    END CASE;
    RETURN text_output;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 3. Main records table
CREATE TABLE IF NOT EXISTS application.records (
    id SERIAL PRIMARY KEY,
    accela_id TEXT UNIQUE NOT NULL,
    permit_number TEXT,
    opened_date TIMESTAMP WITH TIME ZONE,
    last_action_date TIMESTAMP WITH TIME ZONE,
    last_action_info JSONB,
    description TEXT,
    status TEXT,
    raw_data JSONB NOT NULL,
    geom GEOMETRY(Geometry, 4326),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', application.extract_all_text(raw_data))
    ) STORED
);

-- 4. Indexes
CREATE INDEX IF NOT EXISTS idx_records_search ON application.records USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_records_permit_number ON application.records(permit_number);
CREATE INDEX IF NOT EXISTS idx_records_opened_date ON application.records(opened_date);
CREATE INDEX IF NOT EXISTS idx_records_last_action_date ON application.records(last_action_date);
CREATE INDEX IF NOT EXISTS idx_records_geom ON application.records USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_records_description ON application.records(description);

-- 5. Suffix mapping table (Accela abbreviations -> GIS full words)
CREATE TABLE IF NOT EXISTS application.suffix_map (
    accela_val TEXT PRIMARY KEY,
    gis_val TEXT NOT NULL
);

INSERT INTO application.suffix_map (accela_val, gis_val) VALUES
    ('ST', 'STREET'), ('AVE', 'AVENUE'), ('DR', 'DRIVE'), ('BLVD', 'BOULEVARD'),
    ('CT', 'COURT'), ('LN', 'LANE'), ('PL', 'PLACE'), ('RD', 'ROAD'),
    ('TER', 'TERRACE'), ('WAY', 'WAY'), ('CIR', 'CIRCLE'), ('PKWY', 'PARKWAY'),
    ('SQ', 'SQUARE'), ('TRL', 'TRAIL'), ('ALY', 'ALLEY'), ('PT', 'POINT'),
    ('HWY', 'HIGHWAY'), ('COVE', 'COVE'), ('TRCE', 'TRACE'), ('XING', 'CROSSING')
ON CONFLICT (accela_val) DO NOTHING;

-- 6. Ordinal mapping table (word -> numeric ordinals)
CREATE TABLE IF NOT EXISTS application.ordinal_map (
    word_val TEXT PRIMARY KEY,
    num_val TEXT NOT NULL
);

INSERT INTO application.ordinal_map (word_val, num_val) VALUES
    ('FIRST', '1ST'), ('SECOND', '2ND'), ('THIRD', '3RD'), ('FOURTH', '4TH'), ('FIFTH', '5TH'),
    ('SIXTH', '6TH'), ('SEVENTH', '7TH'), ('EIGHTH', '8TH'), ('NINTH', '9TH'), ('TENTH', '10TH'),
    ('ELEVENTH', '11TH'), ('TWELFTH', '12TH'), ('THIRTEENTH', '13TH'), ('FOURTEENTH', '14TH'),
    ('FIFTEENTH', '15TH'), ('SIXTEENTH', '16TH'), ('SEVENTEENTH', '17TH'), ('EIGHTEENTH', '18TH'),
    ('NINETEENTH', '19TH'), ('TWENTIETH', '20TH'), ('TWENTY FIRST', '21ST'), ('TWENTY SECOND', '22ND'),
    ('TWENTY THIRD', '23RD'), ('TWENTY FOURTH', '24TH'), ('TWENTY FIFTH', '25TH'), ('TWENTY SIXTH', '26TH'),
    ('TWENTY SEVENTH', '27TH'), ('TWENTY EIGHTH', '28TH'), ('TWENTY NINTH', '29TH'), ('THIRTIETH', '30TH')
ON CONFLICT (word_val) DO NOTHING;

-- 7. Views for linked data

-- Workflow Histories View
CREATE OR REPLACE VIEW application.view_workflow_histories AS
SELECT
    r.id AS record_db_id,
    r.permit_number,
    w.*
FROM application.records r,
     jsonb_to_recordset(r.raw_data -> 'workflow_histories') AS w(
         "id" text,
         "processCode" text,
         "description" text,
         "lastModifiedDate" text,
         "status" text,
         "comment" text,
         "actionbyUser" jsonb
     );

-- Contacts View
CREATE OR REPLACE VIEW application.view_contacts AS
SELECT
    r.id AS record_db_id,
    r.permit_number,
    c.*
FROM application.records r,
     jsonb_to_recordset(r.raw_data -> 'contacts') AS c(
         "firstName" text,
         "lastName" text,
         "organizationName" text,
         "email" text,
         "phone1" text,
         "type" text,
         "referenceContactId" text
     );

-- Addresses View
CREATE OR REPLACE VIEW application.view_addresses AS
SELECT
    r.id AS record_db_id,
    r.permit_number,
    a.*
FROM application.records r,
     jsonb_to_recordset(r.raw_data -> 'addresses') AS a(
        "streetName" text,
        "streetStart" int,
        "streetEnd" int,
        "streetSuffix" jsonb,
        "streetSuffixDirection" jsonb,
        "postalCode" text
     );

-- Parcels View
CREATE OR REPLACE VIEW application.view_parcels AS
SELECT
    r.id AS record_db_id,
    r.permit_number,
    p.*
FROM application.records r,
     jsonb_to_recordset(r.raw_data -> 'parcels') AS p(
        "parcelNumber" text,
        "isPrimary" text,
        "block" text,
        "lot" text
     );
