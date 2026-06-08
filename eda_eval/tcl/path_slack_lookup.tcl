# Look up the worst-slack timing path between each (start, end) pair in a
# TSV file, against a stage db that's already been loaded.
#
# Companion to worst_logical_paths.tcl: that TCL discovers the top-N worst
# logical groups via iterative masking; this one takes an externally-provided
# list of groups (typically the union from two stages' top-N) and reports the
# worst slack of the matching pair at the loaded stage. Use it to backfill
# the stage that did not surface a given group on its own.
#
# The input "start" / "end" entries are the raw OpenSTA startpoint/endpoint
# names recorded by worst_logical_paths.tcl as start_full / end_full
# (e.g. `core.keymem.key_mem[3][7]$_DFF_P_/D`). They're piped through
# `block_selector` to expand every digit run to "*" and resolve to the
# cells (or ports) of the entire replicated logical block.
#
# Driven by analysis/extract_path_slack_union.py via these env vars:
#   PSL_INPUT_TSV  -- input file, lines of "start_full<TAB>end_full"
#                     (any leading "#" or empty lines are skipped)
#   PSL_OUTPUT_TSV -- output file, "start<TAB>end<TAB>slack_ns" rows;
#                     slack column is "NA" if no path matched
#   PSL_STAGE      -- ORFS stage name (e.g. 1_synth, 6_final) for parasitics
#   PSL_SPEF       -- spef path (empty string if no parasitics file)
#   PSL_SET_RC     -- path to the platform's setRC.tcl

if {![info exists env(PSL_INPUT_TSV)]}  { error "PSL_INPUT_TSV not set" }
if {![info exists env(PSL_OUTPUT_TSV)]} { error "PSL_OUTPUT_TSV not set" }
if {![info exists env(PSL_STAGE)]}      { error "PSL_STAGE not set" }
if {![info exists env(PSL_SET_RC)]}     { error "PSL_SET_RC not set" }

# Re-use setup_parasitics and block_selector from the sibling TCL so the
# wire-RC model and selector expansion match the original report exactly.
source [file join [file dirname [info script]] worst_logical_paths.tcl]

set stage  $env(PSL_STAGE)
set set_rc $env(PSL_SET_RC)
set spef   ""
if {[info exists env(PSL_SPEF)]} { set spef $env(PSL_SPEF) }

setup_parasitics $stage $spef $set_rc

set fin  [open $env(PSL_INPUT_TSV)  r]
set fout [open $env(PSL_OUTPUT_TSV) w]
puts $fout "# start\tend\tslack_ns"

set total 0
set found 0
while {[gets $fin line] >= 0} {
    if {$line eq ""} continue
    if {[string index $line 0] eq "#"} continue
    set parts [split $line "\t"]
    if {[llength $parts] < 2} continue
    set sp [lindex $parts 0]
    set ep [lindex $parts 1]
    incr total

    set sp_sel [block_selector $sp]
    set ep_sel [block_selector $ep]

    if {[llength $sp_sel] == 0 || [llength $ep_sel] == 0} {
        puts $fout "$sp\t$ep\tNA"
        continue
    }

    set p [lindex [find_timing_paths -from $sp_sel -to $ep_sel \
                                     -path_delay max \
                                     -group_path_count 1 \
                                     -endpoint_path_count 1 \
                                     -slack_max 1e30 \
                                     -sort_by_slack] 0]
    if {$p eq ""} {
        puts $fout "$sp\t$ep\tNA"
        continue
    }
    set slack [sta::get_property $p slack]
    puts $fout "$sp\t$ep\t[format %.6f $slack]"
    incr found
}
close $fin
close $fout
puts "PSL: looked up $found/$total paths (stage=$stage)"
