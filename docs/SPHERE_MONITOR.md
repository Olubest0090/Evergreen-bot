# Federal Sphere Monitor — v1

## Notification channels
The sphere uses three separate channels configured per Discord guild:
- `offensive_channel_id`
- `defensive_channel_id`
- `spy_channel_id`

## Registry
A sphere administrator with Discord Administrator permission can add an alliance
using its Politics & War alliance name or numeric ID. The bot resolves the alliance
through the existing P&W GraphQL client and stores its canonical ID and name.

## War monitoring
The monitor checks active wars for every active registered alliance. A war is
classified from the perspective of the registered alliance being queried. The
monitor persists a separate record for each `(guild_id, alliance_id, war_id)` so
that two sphere alliances involved in the same war can each be represented.

## Spy monitoring
The monitor stores a baseline spy count per `(guild_id, nation_id)`. The first
observation only creates a baseline. A lower subsequent count is posted to the
spy channel. The alert explicitly avoids claiming to know who performed the spy
operation because the member roster's spy count does not provide that attribution.

## Rate-limit strategy
The monitor has one loop per bot process, a 120-second interval, a cycle lock,
and reuses the existing P&W client's alliance-war cache. Discord sends occur only
for new war alerts or observed spy-count decreases. Discord mentions are disabled
for these automated messages to avoid accidental mass pings.

## Database migration
Run `database/migrations/001_federal_sphere.sql` in the Supabase SQL Editor before
loading the cog. The migration is additive and does not alter existing tables.
