# Goal - Who Owns Atlanta, similar to NYC who owns what
Generally, follow and group ownership.

[X] Atlanta + Fulton County + Dekalb County

[ ] GA SOS Scraper - in js, need to migrate

Project Dir: /home/jesse/projects/python/who_owns_atl/
	/data  - 
	/data/json/geojson/latest/ 
		Fulton County - Fulton_County_Tax_Parcel.json
		Dekalb County - Dekalb_County_Tax_Parcels.geojson
		 - see (this is a linked repo, don't bother with anything else)
		 	@/tmp_nbh_accela/load_geojson.py


    /docs 
    	- @horizontal-holdings.pdf (HH)
			- pg 4 create workflow/map to follow with addresses

		- Workflow, Address Matching
			- @./docs/workflow_setup_cg.md (suggestions from llm1)
			- @./docs/workflow_setup_gk.md (suggestions from llm2)


- Need to create Network/attach all properties to owners.
	- wtf is this? useful? https://networkx.org/en/
		"uses the networkx Python package to group owner contact names together that share the same registered business address, which also generates an aggregated list of buildings for each grouping of owner names."

Important things:
- Every property has an "ownership group", even if the membership size is "1"
- Be certain to keep a ParcelId we can track back/along with each Tax_Parcel Record as we mangle addresses. 
- Any Scraping should create/save a copy of the data like (permanent) cache so we don't have to rescrape



Start with an Tax_Parcel Record:
	- must be residential
	- must NOT have homestead exemption
	- Every property has an "ownership group", even if the membership size is "1"
	- Be certain to keep a ParcelId we can track back/along with each Tax_Parcel Record as we mangle addresses. 

	- run through HH workflow.
		- stop points (needs fleshing out)
		- what gets sent back through
		- have GA SOS, do we need to go further?


Workflow details:
- SOS data 
	- collect/save details of business addresses outside of Atlanta/Georgia


- "Select"/Assign the "parent" company/DBA for each group
