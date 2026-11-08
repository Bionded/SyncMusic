-- the job an I/O error or write sample came from (the health card links to it); older records have none
ALTER TABLE device_health ADD COLUMN job_id TEXT;
