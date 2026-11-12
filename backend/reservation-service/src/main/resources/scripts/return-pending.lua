-- Gives back the tickets of a hold whose confirmation claimed it but never reached MySQL.
--
-- confirm.lua takes a hold out of the expiry index and leaves availability alone, because the
-- tickets are meant to become sold. When the MySQL write then fails for good - the process died,
-- or the database stayed down - nobody would ever return them. Reconciliation calls this once the
-- hold's deadline is well past, and the state check makes it happen at most once.
--
-- KEYS[1] holdinfo:{eventId}:holdId
-- KEYS[2] avail:{eventId}:categoryId
-- KEYS[3] userqty:{eventId}:userId
--
-- ARGV[1] quantity
--
-- Returns { 'OK' } if this call returned the tickets, { 'NOT_INITIALISED' } if the counter is
-- gone (the rebuild recounts it from MySQL instead), or { 'ALREADY_RECLAIMED' } otherwise.

if redis.call('HGET', KEYS[1], 'state') ~= 'SOLD_PENDING' then
  return { 'ALREADY_RECLAIMED' }
end
-- INCRBY on a missing counter would create one holding just this hold's tickets.
if redis.call('EXISTS', KEYS[2]) == 0 then
  return { 'NOT_INITIALISED' }
end

local quantity = tonumber(ARGV[1])
redis.call('INCRBY', KEYS[2], quantity)
redis.call('DECRBY', KEYS[3], quantity)
redis.call('HSET', KEYS[1], 'state', 'RETURNED')

return { 'OK' }
