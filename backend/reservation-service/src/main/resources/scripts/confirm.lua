-- Claims a hold for a payment that has gone through.
--
-- The same ZREM claim as reclaim.lua, so a confirmation and an expiry arriving in the same
-- instant cannot both act, and winning it is what stops the sweep expiring a hold that has
-- just been paid for.
--
-- What this script deliberately does NOT do:
--
--   * it does not touch avail. The tickets were already subtracted when the hold was taken;
--     they have simply changed from held to sold, so availability must not move.
--   * it does not touch userqty. That counts held AND bought against the five-ticket limit,
--     and buying is exactly what just happened.
--
-- The hold's own key is deleted, because a confirmed hold is finished and must not look
-- confirmable again. From then on only the MySQL row can tell a retry (success) apart from a
-- hold that never existed (Checkout refunds).
--
-- A claim is never undone, because a MySQL write that failed may still have committed. So the
-- claim records its order, and a retry by that same order is let through to write the sale
-- again; whichever of the two lands, the row records it once.
--
-- KEYS[1] zexp:{eventId}:categoryId    the claim
-- KEYS[2] hold:{eventId}:holdId        liveness
-- KEYS[3] holdinfo:{eventId}:holdId    how the hold ended
--
-- ARGV[1] holdId
-- ARGV[2] holdInfoTtlSeconds, for info this script has to write afresh
-- ARGV[3] orderId
--
-- Returns { 'OK' } if this order owns the claim (newly or from an earlier attempt), { 'NOT_ALIVE' }
-- if the hold's key has already died, or { 'ALREADY_RECLAIMED' } if someone else owns this
-- hold's ending.

-- Checked here rather than in Java so there is no window between asking and claiming.
if redis.call('EXISTS', KEYS[2]) == 0 then
  if redis.call('HGET', KEYS[3], 'state') == 'SOLD_PENDING' then
    if redis.call('HGET', KEYS[3], 'order_id') == ARGV[3] then
      return { 'OK' }
    end
    return { 'ALREADY_RECLAIMED' }
  end
  return { 'NOT_ALIVE' }
end

if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then
  return { 'ALREADY_RECLAIMED' }
end

-- Sold, but only once MySQL says CONFIRMED. If that write never lands, reconciliation finds this
-- and gives the tickets back; without it they would stay subtracted for good. So the info is
-- written even if it has gone missing, copied from the hold's own key before that is deleted.
if redis.call('EXISTS', KEYS[3]) == 0 then
  local fields = redis.call('HGETALL', KEYS[2])
  if #fields > 0 then
    redis.call('HSET', KEYS[3], unpack(fields))
  end
  redis.call('EXPIRE', KEYS[3], tonumber(ARGV[2]))
end
redis.call('HSET', KEYS[3], 'state', 'SOLD_PENDING', 'order_id', ARGV[3])
redis.call('DEL', KEYS[2])

return { 'OK' }
