# Conflicting Tesla tariff readbacks

During supervised validation on 1 November 2026, Tesla's cloud returned two
different complete annual tariffs for the same energy site. No credentials or
site identifiers are recorded here.

The tariff command returned HTTP 200, but its expected structured application
acknowledgement was unavailable. One subsequent `tariff_rate` read matched the
submitted tariff; later reads of that endpoint matched the previous tariff.
The controller retained the uncertain intent, stayed in Self-Powered, issued no
new safety lease, and did not resend the command.

Two fresh reads of `site_info.response.tariff_content` then matched the submitted
complete annual configuration, while `tariff_rate.response` continued to return
the previous configuration. A separate `tariff_content_v2` representation in
`site_info` failed the supported clock-range validation. A valid stored tariff
must therefore be distinguished from both a conflicting endpoint and a
malformed alternative representation.

Matching only the current price is insufficient evidence: the previous tariff
already contained the current quoted import and export prices, but used the
older observed pair throughout its unquoted annual planning periods. The new
configuration changed those planning assumptions as well as its quote overlay.
Those unquoted values remain explicit assumptions, not future Optimise quotes.

The semantic hash covers the complete annual import and export schedules,
including leap day. It has no current-time or expiry input. Labels, metadata,
GET/POST wrappers, adjacent equal-rate periods and equivalent season splits do
not affect the digest. Ordinary format normalisation or the passage of time
cannot explain the different digests observed here.

The readback selector prefers a nonempty, structurally valid stored
`site_info.tariff_content`, then a valid stored `tariff_content_v2` when the flat
form is absent. It falls back to `tariff_rate` only when no stored tariff is
present. A meaningful but invalid preferred representation blocks selection;
it is never silently replaced by a conflicting, easier-to-validate response.
The selected source is evidence of the configuration read, not proof of the
tariff engine's effective planning state.

These observations do not establish a particular cache duration, explain the
internal processing delay, prove which cloud representation the Powerwall's
planning engine consumed, or guarantee that a tariff command succeeded merely
because its HTTP status was 200. The recurring controller still requires
matching independent readbacks, fresh source prices, the independent watchdog,
and an actual gateway operating-mode observation before managing Savings.
