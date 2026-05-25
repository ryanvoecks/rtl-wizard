# Extract worst-slack setup paths from a loaded post-route database and
# dump them as a tab-separated table for the Python side to ingest.
#
# Driven by analyse.py via two env vars:
#   ANALYSE_OUT_TSV  -- output file path (required)
#   ANALYSE_POOL     -- find_timing_paths -group_path_count (default 1000)
#
# Each output row is: slack_ns\tstartpoint\tendpoint\tcells
# where "cells" is a "|"-joined list of unique instance names visited on
# the path (path points whose pin has no "/" -- i.e. ports -- are skipped).

if {![info exists env(ANALYSE_OUT_TSV)]} {
    error "ANALYSE_OUT_TSV env var not set"
}
set out_path $env(ANALYSE_OUT_TSV)
set pool 1000
if {[info exists env(ANALYSE_POOL)]} {
    set pool $env(ANALYSE_POOL)
}

# -endpoint_path_count is set equal to the pool size so multiple paths to
# the same endpoint are retained: the per-group `count` in the Python-side
# logical-paths rollup then reflects true path multiplicity through each
# (start_stem, end_stem) pair, not just the number of unique endpoint
# flops. -group_path_count still caps the total at $pool per clock-group.
set paths [find_timing_paths -path_delay max \
                             -group_path_count $pool \
                             -endpoint_path_count $pool]

set fh [open $out_path w]
puts $fh "# slack_ns\tstartpoint\tendpoint\tcells"
foreach p $paths {
    set slack [sta::get_property $p slack]
    set sp [sta::get_property [sta::get_property $p startpoint] full_name]
    set ep [sta::get_property [sta::get_property $p endpoint]   full_name]
    set cells [list]
    foreach pt [sta::get_property $p points] {
        set pin [sta::get_property $pt pin]
        set fn  [sta::get_property $pin full_name]
        set slash [string last "/" $fn]
        if {$slash < 0} { continue }
        lappend cells [string range $fn 0 [expr {$slash - 1}]]
    }
    set cells [lsort -unique $cells]
    puts $fh "[format %.6f $slack]\t$sp\t$ep\t[join $cells |]"
}
close $fh
puts "ANALYSE: wrote [llength $paths] paths to $out_path"
