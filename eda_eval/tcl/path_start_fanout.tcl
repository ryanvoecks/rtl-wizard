# Compute startpoint output-net fanout statistics for each (start, end)
# pair in a TSV file, against a stage db that's already been loaded.
#
# Sister of path_slack_lookup.tcl: instead of returning the worst slack of
# the pair, returns the fanout stats of the union of cells/ports that the
# startpoint's block-glob expands to. The endpoint is ignored here -- the
# input TSV format mirrors path_slack_lookup.tcl so the same union list
# can drive both extractors.
#
# Re-uses `block_selector` from worst_logical_paths.tcl but rolls its own
# port-vs-cell fanout walk for two reasons:
#   (1) `_selector_fanout_stats` over there reports 0 for primary input
#       ports because `get_pins -of_objects $port` returns the port's
#       "input"-direction pseudo-pin, which the helper filters out as
#       non-output. We treat ports specially.
#   (2) Synth often inserts a single-stage buffer between a high-fanout
#       startpoint (a reset, a clock-enable) and its leaf loads. The port
#       net then has literal fanout 1 even though the signal logically
#       drives many flops. To recover the logical fanout, we walk forward
#       through any 1-in/1-out cell (buffer/inverter) and count leaf loads
#       in the cone.
#
# Driven by analysis/extract_path_start_fanout.py via these env vars:
#   PSF_INPUT_TSV  -- input file, "start_full<TAB>end_full" rows
#   PSF_OUTPUT_TSV -- output file, "start<TAB>end<TAB>n_cells<TAB>
#                     n_output_nets<TAB>mean_fanout<TAB>max_fanout" rows;
#                     "NA" columns if the startpoint glob matched nothing
#   PSF_STAGE      -- ORFS stage name (e.g. 1_synth) for parasitics
#   PSF_SPEF       -- spef path (empty if no parasitics file)
#   PSF_SET_RC     -- path to the platform's setRC.tcl

if {![info exists env(PSF_INPUT_TSV)]}  { error "PSF_INPUT_TSV not set" }
if {![info exists env(PSF_OUTPUT_TSV)]} { error "PSF_OUTPUT_TSV not set" }
if {![info exists env(PSF_STAGE)]}      { error "PSF_STAGE not set" }
if {![info exists env(PSF_SET_RC)]}     { error "PSF_SET_RC not set" }

source [file join [file dirname [info script]] worst_logical_paths.tcl]

set stage  $env(PSF_STAGE)
set set_rc $env(PSF_SET_RC)
set spef   ""
if {[info exists env(PSF_SPEF)]} { set spef $env(PSF_SPEF) }
setup_parasitics $stage $spef $set_rc

# Effective leaf-load count rooted at `net`. A "buffer" cell here is any
# single-input single-output cell (buffer, inverter, sometimes a clock
# gater stub depending on the library) -- we treat them as transparent
# and recurse through them, so a port -> buffer -> N flops chain counts
# as N, not 1. Non-buffer cells, and any pin without a backing cell (top-
# level output port pseudo-pins), terminate the walk as a single leaf.
#
# The `visited` array (passed by upvar) guards against the rare driver-
# cycle in pre-PnR netlists and keeps recursion bounded.
proc _effective_fanout {net visited_var} {
    upvar 1 $visited_var v
    set key [sta::get_property $net full_name]
    if {[info exists v($key)]} { return 0 }
    set v($key) 1
    set leaves 0
    foreach pin [get_pins -of_objects $net] {
        if {[sta::get_property $pin direction] ne "input"} continue
        set cell ""
        catch {set cell [get_cells -of_objects $pin]}
        if {$cell eq ""} {
            # Top-level output port pseudo-pin or otherwise no driving cell.
            incr leaves
            continue
        }
        set in_pins [list]
        set out_pins [list]
        foreach p [get_pins -of_objects $cell] {
            set dir [sta::get_property $p direction]
            if {$dir eq "input"} { lappend in_pins $p }
            if {$dir eq "output"} { lappend out_pins $p }
        }
        if {[llength $in_pins] == 1 && [llength $out_pins] == 1} {
            set out_net [get_nets -of_objects [lindex $out_pins 0]]
            if {$out_net ne ""} {
                set leaves [expr {$leaves + [_effective_fanout $out_net v]}]
                continue
            }
        }
        incr leaves
    }
    return $leaves
}

# Walk a selector and tally per-driver-net effective fanout. `is_port` is
# true when the selector came from `get_ports` (driver net == the port's
# net, found by name since `get_nets -of_objects $port` errors with
# STA-0100); false when from `get_cells` (driver nets are the outputs of
# every output pin on each cell). Returns {n_driver_nets total max}.
proc _start_fanout_stats {sel is_port} {
    set n 0
    set total 0
    set fmax 0
    foreach obj $sel {
        set nets [list]
        if {$is_port} {
            set name [sta::get_property $obj full_name]
            set net_sel [get_nets -quiet $name]
            foreach net $net_sel { lappend nets $net }
        } else {
            set obj_pins [list]
            catch {set obj_pins [get_pins -of_objects $obj]}
            foreach pin $obj_pins {
                if {[sta::get_property $pin direction] ne "output"} continue
                set net [get_nets -of_objects $pin]
                if {$net ne ""} { lappend nets $net }
            }
        }
        foreach net $nets {
            array unset visited
            array set visited {}
            set f [_effective_fanout $net visited]
            incr n
            set total [expr {$total + $f}]
            if {$f > $fmax} { set fmax $f }
        }
    }
    return [list $n $total $fmax]
}

set fin  [open $env(PSF_INPUT_TSV)  r]
set fout [open $env(PSF_OUTPUT_TSV) w]
puts $fout "# start\tend\tn_cells\tn_output_nets\tmean_fanout\tmax_fanout"

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
    if {[llength $sp_sel] == 0} {
        puts $fout "$sp\t$ep\tNA\tNA\tNA\tNA"
        continue
    }

    set is_port [expr {![string match "*/*" $sp]}]
    set n_cells [llength $sp_sel]
    lassign [_start_fanout_stats $sp_sel $is_port] n_nets total_fanout fmax
    if {$n_nets > 0} {
        set mean [expr {double($total_fanout) / $n_nets}]
    } else {
        set mean 0.0
    }
    puts $fout "$sp\t$ep\t$n_cells\t$n_nets\t[format %.3f $mean]\t$fmax"
    incr found
}
close $fin
close $fout
puts "PSF: looked up $found/$total starts (stage=$stage)"
