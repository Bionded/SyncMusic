-- USB disks now accept any format by default; disks registered before keep the formats they used
UPDATE devices
SET config = json_set(config, '$.settings.formats', json('["aac","flac","mp3","ogg","opus","wav"]'))
WHERE type = 'flashdrive'
  AND json_type(config, '$.settings') = 'object'
  AND json_type(config, '$.settings.formats') IS NULL;
