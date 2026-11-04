# Matchup statistics

Terms used when deriving batter-versus-bowler statistics from ball-by-ball cricket data.

## Language

**Mandatory powerplay**:
The interval whose `pp_overs_from` is ball 1. Its `pp_*` key need not be `pp_1`. It is the only powerplay interval used for matchup phase classification.

**Nonstandard powerplay**:
A valid mandatory powerplay whose final ball differs from the expected boundary for a regular innings. A reduced innings may legitimately have a shorter powerplay.

**Expected powerplay**:
The mandatory powerplay boundary used to validate a regular innings. A league-specific phase definition takes precedence over the match format's default boundary.

**League phase definition**:
A set of phase boundaries that applies to full-length innings in a named league. BBL has a four-over powerplay and death overs starting at ball 91.

**Allotted overs**:
The innings limit used for phase classification, distinct from overs actually completed. An innings that ends early still uses its allotted limit.

**Reduced-overs match**:
A match whose innings limits are supplied as a complete set, one limit for every scorecard innings. A match without such a set follows the normal phase definition.

**Death overs**:
The last phase of an innings when balls remain after powerplay. Regular innings use fixed boundaries; reduced innings use either every post-powerplay ball or the final 25% of allotted balls, rounded up, with powerplay taking precedence.

**Phase boundary**:
The inclusive first or last ball number of a phase in an innings, independent of which batter and bowler appeared in that phase.
