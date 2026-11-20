-- Wi-Fi clients: a copy of the app's own settings (JSON object as the app reported it), handed back when the
-- same player pairs again after its app data was reset
ALTER TABLE client_pairings ADD COLUMN app_settings TEXT;
