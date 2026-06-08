# Top-K corrected-slack logical-blocks extractor.
#
# Reads the predictor's per-port-class I/O correction:
#   corr_slack = synth_slack + beta[port_class]
# Pulls paths from the worst-path queue in batches of `batch_size`, dedups
# at the block level, false-paths the batch's block pairs, and terminates
# as soon as the next raw slack cannot enter the top-N under correction
# (because the worst correction min(beta) applied to it would still leave
# its corrected slack above the current rank-N threshold).
#
# Sources worst_logical_paths.tcl for setup_parasitics, block_base,
# block_selector.

source [file join [file dirname [info script]] worst_logical_paths.tcl]


proc _build_port_dir_map {} {
    set m [dict create]
    foreach p [get_ports *] {
        dict set m [sta::get_property $p full_name] \
            [sta::get_property $p direction]
    }
    return $m
}

proc _port_direction {port_dirs full_name} {
    if {[string match "*/*" $full_name]} { return "" }
    if {[string match "*.*" $full_name]} { return "" }
    if {[dict exists $port_dirs $full_name]} {
        return [dict get $port_dirs $full_name]
    }
    return ""
}

proc _classify_path {port_dirs sp ep} {
    set sd [_port_direction $port_dirs $sp]
    set ed [_port_direction $port_dirs $ep]
    set is_in  [expr {$sd eq "input"  || $sd eq "inout"}]
    set is_out [expr {$ed eq "output" || $ed eq "inout"}]
    if {$is_in && $is_out} { return "both" }
    if {$is_in}            { return "input_only" }
    if {$is_out}           { return "output_only" }
    return "internal"
}


proc find_top_corrected_blocks {out_path top_n stage spef set_rc top_module
                                b_input b_output b_internal
                                {batch_size 25} {skip_async 1}
                                {include_debug 0}} {
    setup_parasitics $stage $spef $set_rc

    set b_both [expr {$b_input + $b_output - $b_internal}]
    array set BETA [list \
        input_only  $b_input  \
        output_only $b_output \
        internal    $b_internal \
        both        $b_both]
    set min_beta $b_output
    foreach v [list $b_input $b_internal $b_both] {
        if {$v < $min_beta} { set min_beta $v }
    }

    set port_dirs [_build_port_dir_map]

    # top_list entries: {corr_slack synth_slack sp_base ep_base port_class}
    # Sorted ascending by corr_slack so the rank-N (worst-allowed) threshold
    # is at the tail.
    set top_list [list]
    set T_corr 1e30
    set seen_blocks [dict create]
    set iterations 0
    set n_pulled 0
    set stop 0

    while {!$stop} {
        incr iterations
        set paths [find_timing_paths -path_delay max \
                                     -group_path_count $batch_size \
                                     -endpoint_path_count 1 \
                                     -slack_max 1e30 \
                                     -sort_by_slack]
        if {[llength $paths] == 0} break

        # batch_masks is keyed by the actual block-glob string pair so that
        # different yosys cell-type variants of the same logical block
        # (DFF / DFFE / DFFSR) each contribute their own false-path mask --
        # block_base alone would dedup these into one and let the unmasked
        # variants surface infinitely.
        set batch_masks [dict create]
        foreach p $paths {
            incr n_pulled
            set s [sta::get_property $p slack]

            # Early termination: even the worst-case beta cannot pull this
            # raw slack below the current rank-N corrected threshold, and
            # all subsequent paths in the queue have even higher raw slack.
            if {$s + $min_beta >= $T_corr} {
                set stop 1
                break
            }

            set sp [sta::get_property [sta::get_property $p startpoint] full_name]
            set ep [sta::get_property [sta::get_property $p endpoint]   full_name]

            set sp_inst [expr {[string match "*/*" $sp] ? [regsub {/[^/]+$} $sp {}] : $sp}]
            set ep_inst [expr {[string match "*/*" $ep] ? [regsub {/[^/]+$} $ep {}] : $ep}]
            set mask_key [list [block_glob $sp_inst] [block_glob $ep_inst]]
            if {![dict exists $batch_masks $mask_key]} {
                dict set batch_masks $mask_key [list $sp $ep]
            }

            set block_key [list [block_base $sp] [block_base $ep]]
            if {[dict exists $seen_blocks $block_key]} { continue }

            if {$skip_async} {
                set slash [string last "/" $ep]
                if {$slash >= 0} {
                    set pin [string range $ep [expr {$slash + 1}] end]
                    if {$pin ne "D"} { continue }
                }
            }

            set pc [_classify_path $port_dirs $sp $ep]
            set s_corr [expr {$s + $BETA($pc)}]

            if {[llength $top_list] < $top_n || $s_corr < $T_corr} {
                lassign $block_key sp_base ep_base
                lappend top_list [list $s_corr $s $sp_base $ep_base $pc]
                set top_list [lsort -real -index 0 $top_list]
                if {[llength $top_list] > $top_n} {
                    set top_list [lrange $top_list 0 [expr {$top_n - 1}]]
                }
                if {[llength $top_list] >= $top_n} {
                    set T_corr [lindex [lindex $top_list end] 0]
                }
                dict set seen_blocks $block_key 1
            }
        }

        dict for {k v} $batch_masks {
            lassign $v sp ep
            set_false_path -from [block_selector $sp] -to [block_selector $ep]
        }
    }

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    if {$include_debug} {
        puts $fh "# beta_input\t[format %.4f $b_input]"
        puts $fh "# beta_output\t[format %.4f $b_output]"
        puts $fh "# beta_internal\t[format %.4f $b_internal]"
        puts $fh "# beta_both\t[format %.4f $b_both]"
        puts $fh "# batch_size\t$batch_size"
        puts $fh "# skip_async\t$skip_async"
        puts $fh "# iterations\t$iterations"
        puts $fh "# n_pulled\t$n_pulled"
        puts $fh "# rank\tworst_slack_ns\tsynth_slack_ns\tport_class\tstart\tend"
    } else {
        puts $fh "# rank\tworst_slack_ns\tstart\tend"
    }
    set rank 0
    foreach entry $top_list {
        lassign $entry s_corr s sp_base ep_base pc
        incr rank
        if {$include_debug} {
            puts $fh "$rank\t[format %.4f $s_corr]\t[format %.4f $s]\t$pc\t$sp_base\t$ep_base"
        } else {
            puts $fh "$rank\t[format %.4f $s_corr]\t$sp_base\t$ep_base"
        }
    }
    close $fh
    puts "FTC: wrote $rank entries (pulled $n_pulled, iters $iterations) to $out_path"
}
