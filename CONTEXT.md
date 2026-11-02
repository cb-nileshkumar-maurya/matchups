# Matchup statistics

Terms used when deriving batter-versus-bowler statistics from ball-by-ball cricket data.

## Language

**Mandatory powerplay**:
The interval whose `pp_overs_from` is ball 1. Its `pp_*` key need not be `pp_1`. It is the only powerplay interval used for matchup phase classification.

**Nonstandard powerplay**:
A valid mandatory powerplay whose final ball differs from the standard boundary for its match format. Its presence alone does not establish the innings' allotted overs.

**Unclassified phase**:
An innings interval whose deliveries cannot be assigned a trustworthy named phase from the available boundaries. It is represented as `NA` in matchup statistics.

**Allotted overs**:
The innings limit used for phase classification, distinct from overs actually completed. An innings that ends early still uses its allotted limit.

**Reduced-overs match**:
A match whose innings limits are supplied as a complete set, one limit for every scorecard innings. A match without such a set follows the normal phase definition.

**Death overs**:
For a reduced-overs innings, the final 20% of the balls remaining after the mandatory powerplay, rounded up to a whole ball. Regular innings use their fixed format boundaries. A reduced-overs allotment must leave balls after the mandatory powerplay.

**Phase boundary**:
The inclusive first or last ball number of a phase in an innings, independent of which batter and bowler appeared in that phase.
