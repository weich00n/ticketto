-- One-off: give the events that were published before categories existed a category.
-- Run once, after migration 0003 has been applied (the `migrate` job), against db_event.
--
-- Safe to run twice: each statement only fills a category that is still empty, so it
-- never overwrites one an organiser chose. Change a value here before running if it
-- is wrong; the allowed values are Concerts, Theatre, Festivals, Sports, Conferences.

UPDATE events SET category = 'Concerts'
 WHERE category IS NULL AND event_id = 'af421f79-10cd-49c9-9cdb-97645fb7811e';  -- Talk on Corners: The World Tour

UPDATE events SET category = 'Theatre'
 WHERE category IS NULL AND event_id = '409309f8-fcb4-4af1-b58b-a9068701a0f6';  -- Harry Potter: A Chronicle of Magic

UPDATE events SET category = 'Concerts'
 WHERE category IS NULL AND event_id = '4b6832b6-84b2-471d-9e40-e26e97cafb76';  -- The Unravelled Tour

-- Check: every published event should now have a category.
SELECT title, status, category FROM events ORDER BY created_at;
