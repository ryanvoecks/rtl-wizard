# Extract per-path cell-arc / net-arc delay split for the worst-N setup paths
# in a loaded post-route database.
#
# For each path, walk its points and split incremental arrival into:
#   cell delay -- arcs terminating at an output pin (gate input -> output, or
#                 clock-to-Q)
#   wire delay -- arcs terminating at an input pin  (driver output -> load)
# The first point has no incoming arc and is skipped (its arrival is the
# clock arrival at the launching register or input port).
#
# Driven by analysis/extract_cell_wire_split.py via two env vars:
#   ANALYSE_OUT_TSV  -- output file path (required)
#   ANALYSE_POOL     -- find_timing_paths -group_path_count (default 1000)
#
# Wire delays are only meaningful when post-route parasitics (SPEF) have been
# annotated; check report_parasitic_annotation before trusting them.

if {![info exists env(ANALYSE_OUT_TSV)]} {
    error "ANALYSE_OUT_TSV env var not set"
}
set out_path $env(ANALYSE_OUT_TSV)
set pool 1000
if {[info exists env(ANALYSE_POOL)]} {
    set pool $env(ANALYSE_POOL)
}

set paths [find_timing_paths -path_delay max \
                             -group_path_count $pool \
                             -endpoint_path_count $pool]

set fh [open $out_path w]
puts $fh "# slack_ns\tcell_delay_ns\twire_delay_ns\tstartpoint\tendpoint"
foreach p $paths {
    set slack [sta::get_property $p slack]
    set sp [sta::get_property [sta::get_property $p startpoint] full_name]
    set ep [sta::get_property [sta::get_property $p endpoint]   full_name]
    set cell_total 0.0
    set wire_total 0.0
    set prev_arrival 0.0
    set first 1
    foreach pt [sta::get_property $p points] {
        set arrival [sta::get_property $pt arrival]
        if {$first} {
            set first 0
            set prev_arrival $arrival
            continue
        }
        set delta [expr {$arrival - $prev_arrival}]
        set prev_arrival $arrival
        set pin [sta::get_property $pt pin]
        set dir [sta::get_property $pin direction]
        if {$dir eq "output"} {
            set cell_total [expr {$cell_total + $delta}]
        } elseif {$dir eq "input"} {
            set wire_total [expr {$wire_total + $delta}]
        }
    }
    puts $fh "[format %.6f $slack]\t[format %.6f $cell_total]\t[format %.6f $wire_total]\t$sp\t$ep"
}
close $fh
puts "CWS: wrote [llength $paths] paths to $out_path"
