# Find the top-N worst *logical* paths or *logical blocks* from a loaded timing
# database.
#
# A logical PATH is one n-bit register/IO bus to another -- bit-level paths
# between the same (source-bus, dest-bus) pair collapse to one entry. The bus
# indices "[N]" are normalised to positional placeholders "[I]", "[J]", ...
#
# A logical BLOCK is the same idea taken further: every numeric run in the
# instance name (not just bracketed indices) is normalised, so the eight
# replicated submodules `dct_block_0..7.dct_unit_0..7.macu.mult_res[N]` all
# collapse to one entry `dct_block_I.dct_unit_J.macu.mult_res[K]`. Use this
# when the design replicates a block by parameter and you want one entry per
# block kind rather than one per instance.
#
# Strategy (both modes): iteratively pull the single worst remaining path,
# record its (source -> dest) pair, then set_false_path that pair so the next
# iteration surfaces a new distinct one. In "blocks" mode the false-path glob
# masks every instance of the replicated block at once.
#
# Source this file into an openroad session that already has the db/sdc/liberty
# loaded, then call either:
#   find_worst_logical_paths  <out_path> <top_n> <stage> <spef> <set_rc> <top_module>
#   find_worst_logical_blocks <out_path> <top_n> <stage> <spef> <set_rc> <top_module>
# Both establish the interconnect parasitics for the stage (the .odb carries
# none) before searching.


# Install the interconnect parasitics appropriate to the flow `stage`.
proc setup_parasitics {stage spef set_rc} {
    switch -- $stage {
        6_final {
            read_spef $spef
        }
        5_route {
            source $set_rc
            estimate_parasitics -global_routing
        }
        3_place -
        4_cts {
            source $set_rc
            estimate_parasitics -placement
        }
    }
}

# Replace each successive bracketed-digit run "[N]" with a positional
# placeholder "[I]", "[J]", ... -- the `paths` mode collapse.
proc index_template {name} {
    set letters {I J K L M N O P Q R S T U V W X Y Z}
    set out ""
    set rest $name
    set i 0
    while {[regexp -indices {\[[0-9]+\]} $rest m]} {
        lassign $m s e
        append out [string range $rest 0 [expr {$s - 1}]]
        if {$i < [llength $letters]} {
            append out "\[[lindex $letters $i]\]"
        } else {
            append out {[?]}
        }
        set rest [string range $rest [expr {$e + 1}] end]
        incr i
    }
    append out $rest
    return $out
}

# Replace each successive digit run with a positional placeholder letter --
# the `blocks` mode collapse. Any surrounding brackets stay in place, so
# "dct_block_6.dct_unit_2.mult_res[3]" -> "dct_block_I.dct_unit_J.mult_res[K]".
proc block_template {name} {
    set letters {I J K L M N O P Q R S T U V W X Y Z}
    set out ""
    set rest $name
    set i 0
    while {[regexp -indices {[0-9]+} $rest m]} {
        lassign $m s e
        append out [string range $rest 0 [expr {$s - 1}]]
        if {$i < [llength $letters]} {
            append out [lindex $letters $i]
        } else {
            append out "?"
        }
        set rest [string range $rest [expr {$e + 1}] end]
        incr i
    }
    append out $rest
    return $out
}

# full pin/port name -> logical base for "paths" mode (drop "/<pin>" suffix,
# strip yosys $... tail, normalise bus indices).
proc reg_base {full} {
    set name [regsub {/[^/]+$} $full {}]
    set name [regsub {\$[^/.]*$} $name {}]
    return [index_template $name]
}

# full pin/port name -> logical base for "blocks" mode (same suffix stripping,
# then normalise every numeric run).
proc block_base {full} {
    set name [regsub {/[^/]+$} $full {}]
    set name [regsub {\$[^/.]*$} $name {}]
    return [block_template $name]
}

# instance/port name -> a glob matching the whole logical bus (paths mode).
proc bus_glob {name} {
    return [regsub -all {\[[0-9]+\]} $name {[*]}]
}

# instance/port name -> a glob matching every replicated instance of the
# block as well as every bit of the bus (blocks mode). Every digit run
# becomes "*".
proc block_glob {name} {
    return [regsub -all {[0-9]+} $name {*}]
}

# full pin/port name -> a collection of all cells/ports in the matching bus.
proc path_selector {full} {
    if {[string match "*/*" $full]} {
        set inst [regsub {/[^/]+$} $full {}]
        return [get_cells [bus_glob $inst]]
    }
    return [get_ports [bus_glob $full]]
}

# full pin/port name -> a collection covering every replicated instance.
proc block_selector {full} {
    if {[string match "*/*" $full]} {
        set inst [regsub {/[^/]+$} $full {}]
        return [get_cells [block_glob $inst]]
    }
    return [get_ports [block_glob $full]]
}

# Shared core for `paths` and `blocks` modes. Iteratively pulls the worst
# remaining path, writes a row, and masks the source/dest pair.
#
# skip_async=1 quarantines endpoints whose pin name is not "D" (RN/SN, SI/SE,
# A1/A2, E, etc.) -- those paths are still false-pathed so they don't keep
# surfacing, just not written.
# include_full=1 adds start_full / end_full columns; default off keeps the
# original 4-column rpt schema byte-for-byte.
proc _find_worst_logical {out_path top_n stage spef set_rc top_module kind {skip_async 0} {include_full 0}} {
    setup_parasitics $stage $spef $set_rc

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    if {$kind ne "paths"}     { puts $fh "# kind\t$kind" }
    if {$skip_async ne "0"}   { puts $fh "# skip_async\t$skip_async" }
    if {$include_full ne "0"} { puts $fh "# include_full\t$include_full" }
    if {$include_full} {
        puts $fh "# rank\tworst_slack_ns\tstart\tend\tstart_full\tend_full"
    } else {
        puts $fh "# rank\tworst_slack_ns\tstart\tend"
    }

    set written 0
    set tried 0
    # Cap iterations so a pathologically-async design can't spin forever.
    set max_tries [expr {$top_n * 20 + 2000}]
    while {$written < $top_n && $tried < $max_tries} {
        incr tried
        set p [lindex [find_timing_paths -path_delay max \
                                         -group_path_count 1 \
                                         -endpoint_path_count 1 \
                                         -slack_max 1e30 \
                                         -sort_by_slack] 0]
        if {$p eq ""} break

        set sp [sta::get_property [sta::get_property $p startpoint] full_name]
        set ep [sta::get_property [sta::get_property $p endpoint]   full_name]
        set slack [sta::get_property $p slack]

        if {$kind eq "blocks"} {
            set sp_base [block_base $sp]
            set ep_base [block_base $ep]
            set sp_sel  [block_selector $sp]
            set ep_sel  [block_selector $ep]
        } else {
            set sp_base [reg_base $sp]
            set ep_base [reg_base $ep]
            set sp_sel  [path_selector $sp]
            set ep_sel  [path_selector $ep]
        }

        set is_async 0
        if {$skip_async} {
            set slash_pos [string last "/" $ep]
            if {$slash_pos >= 0} {
                set pin [string range $ep [expr {$slash_pos + 1}] end]
                if {$pin ne "D"} { set is_async 1 }
            }
        }

        if {$is_async} {
            set_false_path -from $sp_sel -to $ep_sel
            continue
        }

        incr written
        if {$include_full} {
            puts $fh "$written\t[format %.4f $slack]\t$sp_base\t$ep_base\t$sp\t$ep"
        } else {
            puts $fh "$written\t[format %.4f $slack]\t$sp_base\t$ep_base"
        }
        # Mask this exact source -> dest group so the next pull surfaces a
        # new one.
        set_false_path -from $sp_sel -to $ep_sel
    }
    close $fh
    puts "WLP: wrote $written logical $kind to $out_path"
}

proc find_worst_logical_paths {out_path top_n stage spef set_rc top_module {skip_async 0} {include_full 0}} {
    _find_worst_logical $out_path $top_n $stage $spef $set_rc $top_module paths $skip_async $include_full
}

proc find_worst_logical_blocks {out_path top_n stage spef set_rc top_module {skip_async 0} {include_full 0}} {
    _find_worst_logical $out_path $top_n $stage $spef $set_rc $top_module blocks $skip_async $include_full
}


# Batched variant of `_find_worst_logical`. Pulls `batch_size` paths per
# `find_timing_paths` call rather than 1; dedupes the batch in python's
# `seen` dict and applies one `set_false_path` per unique (selector pair)
# at the end of each batch. Functionally equivalent to the unbatched proc
# but with O(top_n / batch_size) STA queries instead of O(top_n) -- the
# same pattern as worst_corrected_blocks.tcl.
proc _find_worst_logical_batched {out_path top_n stage spef set_rc top_module kind {batch_size 25} {include_full 0}} {
    setup_parasitics $stage $spef $set_rc

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    if {$kind ne "paths"}     { puts $fh "# kind\t$kind" }
    if {$batch_size ne "1"}   { puts $fh "# batch_size\t$batch_size" }
    if {$include_full ne "0"} { puts $fh "# include_full\t$include_full" }
    if {$include_full} {
        puts $fh "# rank\tworst_slack_ns\tstart\tend\tstart_full\tend_full"
    } else {
        puts $fh "# rank\tworst_slack_ns\tstart\tend"
    }

    set written 0
    set seen [dict create]
    set iter 0
    # Cap to keep an empty/async-dominated queue from spinning forever.
    set max_iters [expr {$top_n * 2 + 500}]
    while {$written < $top_n && $iter < $max_iters} {
        incr iter
        set paths [find_timing_paths -path_delay max \
                                     -group_path_count $batch_size \
                                     -endpoint_path_count 1 \
                                     -slack_max 1e30 \
                                     -sort_by_slack]
        if {[llength $paths] == 0} break

        # batch_masks: keyed by the actual selector glob pair so different
        # yosys cell-type variants of the same logical block (DFF / DFFE /
        # DFFSR) each contribute their own false-path mask. Value is the
        # representative {sp ep} pair used to build the selectors.
        set batch_masks [dict create]
        foreach p $paths {
            if {$written >= $top_n} break

            set sp [sta::get_property [sta::get_property $p startpoint] full_name]
            set ep [sta::get_property [sta::get_property $p endpoint]   full_name]
            set slack [sta::get_property $p slack]

            if {$kind eq "blocks"} {
                set sp_base [block_base $sp]
                set ep_base [block_base $ep]
                set sp_inst [expr {[string match "*/*" $sp] ? [regsub {/[^/]+$} $sp {}] : $sp}]
                set ep_inst [expr {[string match "*/*" $ep] ? [regsub {/[^/]+$} $ep {}] : $ep}]
                set mask_key [list [block_glob $sp_inst] [block_glob $ep_inst]]
            } else {
                set sp_base [reg_base $sp]
                set ep_base [reg_base $ep]
                set sp_inst [expr {[string match "*/*" $sp] ? [regsub {/[^/]+$} $sp {}] : $sp}]
                set ep_inst [expr {[string match "*/*" $ep] ? [regsub {/[^/]+$} $ep {}] : $ep}]
                set mask_key [list [bus_glob $sp_inst] [bus_glob $ep_inst]]
            }
            if {![dict exists $batch_masks $mask_key]} {
                dict set batch_masks $mask_key [list $sp $ep]
            }

            set key [list $sp_base $ep_base]
            if {[dict exists $seen $key]} continue
            dict set seen $key 1
            incr written
            if {$include_full} {
                puts $fh "$written\t[format %.4f $slack]\t$sp_base\t$ep_base\t$sp\t$ep"
            } else {
                puts $fh "$written\t[format %.4f $slack]\t$sp_base\t$ep_base"
            }
        }

        dict for {k v} $batch_masks {
            lassign $v sp ep
            if {$kind eq "blocks"} {
                set_false_path -from [block_selector $sp] -to [block_selector $ep]
            } else {
                set_false_path -from [path_selector $sp] -to [path_selector $ep]
            }
        }
    }
    close $fh
    puts "WLP: wrote $written logical $kind (batched=$batch_size, iters=$iter) to $out_path"
}

proc find_worst_logical_paths_batched {out_path top_n stage spef set_rc top_module {batch_size 25} {include_full 0}} {
    _find_worst_logical_batched $out_path $top_n $stage $spef $set_rc $top_module paths $batch_size $include_full
}

proc find_worst_logical_blocks_batched {out_path top_n stage spef set_rc top_module {batch_size 25} {include_full 0}} {
    _find_worst_logical_batched $out_path $top_n $stage $spef $set_rc $top_module blocks $batch_size $include_full
}


# Per-net fanout = number of load (input-direction) pins on the net. The driver
# (output pin or input port) is excluded.
proc _net_fanout {net} {
    set fanout 0
    foreach pin [get_pins -of_objects $net] {
        if {[sta::get_property $pin direction] eq "input"} {
            incr fanout
        }
    }
    return $fanout
}

# For a list of selector objects (cells from get_cells or ports from get_ports),
# return {n_output_nets total_fanout max_fanout} across every output-driven net
# the selector spans. Cells contribute via their output pins; top-level input
# ports contribute via the net they drive directly.
proc _selector_fanout_stats {sel} {
    set n 0
    set total 0
    set fmax 0
    foreach obj $sel {
        set output_nets [list]
        # get_pins -of_objects throws STA-0100 for Port objects, so guard the
        # cell path with catch and fall back to the port path on failure.
        set obj_pins [list]
        catch {set obj_pins [get_pins -of_objects $obj]}
        if {[llength $obj_pins] > 0} {
            foreach pin $obj_pins {
                if {[sta::get_property $pin direction] ne "output"} continue
                set net [get_nets -of_objects $pin]
                if {$net ne ""} { lappend output_nets $net }
            }
        } else {
            # Top-level port: treat its connected net as the driver net.
            set net ""
            catch {set net [get_nets -of_objects $obj]}
            if {$net ne ""} { lappend output_nets $net }
        }
        foreach net $output_nets {
            set f [_net_fanout $net]
            incr n
            set total [expr {$total + $f}]
            if {$f > $fmax} { set fmax $f }
        }
    }
    return [list $n $total $fmax]
}

# Walks the same worst-path queue as find_worst_logical_blocks but, instead of
# only recording the (start, end) pair, also computes the fanout statistics of
# the union of cells matched by the block selectors. One row per rank.
proc find_block_fanouts {out_path top_n stage spef set_rc top_module} {
    setup_parasitics $stage $spef $set_rc

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    puts $fh "# kind\tblock_fanout"
    puts $fh "# rank\tworst_slack_ns\tstart\tend\tn_cells\tn_output_nets\tmean_fanout\tmax_fanout"

    set written 0
    for {set i 1} {$i <= $top_n} {incr i} {
        set p [lindex [find_timing_paths -path_delay max \
                                         -group_path_count 1 \
                                         -endpoint_path_count 1 \
                                         -slack_max 1e30 \
                                         -sort_by_slack] 0]
        if {$p eq ""} break

        set sp [sta::get_property [sta::get_property $p startpoint] full_name]
        set ep [sta::get_property [sta::get_property $p endpoint]   full_name]
        set slack [sta::get_property $p slack]

        set sp_base [block_base $sp]
        set ep_base [block_base $ep]
        set sp_sel  [block_selector $sp]
        set ep_sel  [block_selector $ep]

        # Combined fanout across cells covered by either block selector.
        set n_cells [expr {[llength $sp_sel] + [llength $ep_sel]}]
        lassign [_selector_fanout_stats $sp_sel] sn st smax
        lassign [_selector_fanout_stats $ep_sel] en et emax
        set n_nets [expr {$sn + $en}]
        set total  [expr {$st + $et}]
        set fmax   [expr {$smax > $emax ? $smax : $emax}]
        set mean   [expr {$n_nets > 0 ? double($total) / $n_nets : 0.0}]

        puts $fh "$i\t[format %.4f $slack]\t$sp_base\t$ep_base\t$n_cells\t$n_nets\t[format %.3f $mean]\t$fmax"
        incr written

        # Mask this pair so the next pull surfaces a fresh logical block group.
        set_false_path -from $sp_sel -to $ep_sel
    }
    close $fh
    puts "BFO: wrote $written block-fanout rows to $out_path"
}

# Same block-discovery loop as find_block_fanouts, but for each discovered
# (sp_sel, ep_sel) block also enumerates every timing path between the two
# selectors (one worst path per endpoint by default) and emits one row per
# (block_rank, path_idx, net_on_path). The fanout value is the load-pin count
# of the driver net for each output pin along the path -- so a net appears
# once per path that traverses it (i.e. the per-path-decomposition view).
proc find_block_path_net_fanouts {out_path top_n stage spef set_rc top_module
                                  {endpoint_path_count 1}
                                  {group_path_count 100000}} {
    setup_parasitics $stage $spef $set_rc

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    puts $fh "# kind\tblock_path_net_fanout"
    puts $fh "# endpoint_path_count\t$endpoint_path_count"
    puts $fh "# group_path_count\t$group_path_count"
    puts $fh "# rank\tpath_idx\tnet_name\tfanout"

    set written 0
    for {set i 1} {$i <= $top_n} {incr i} {
        set p0 [lindex [find_timing_paths -path_delay max \
                                          -group_path_count 1 \
                                          -endpoint_path_count 1 \
                                          -slack_max 1e30 \
                                          -sort_by_slack] 0]
        if {$p0 eq ""} break

        set sp [sta::get_property [sta::get_property $p0 startpoint] full_name]
        set ep [sta::get_property [sta::get_property $p0 endpoint]   full_name]
        set sp_sel [block_selector $sp]
        set ep_sel [block_selector $ep]

        # Enumerate every timing path inside this block (sp_sel -> ep_sel).
        # endpoint_path_count = 1 gives one worst path per endpoint; raise it
        # to capture combinational alternatives to the same endpoint.
        set block_paths [find_timing_paths -path_delay max \
                                           -from $sp_sel -to $ep_sel \
                                           -group_path_count $group_path_count \
                                           -endpoint_path_count $endpoint_path_count \
                                           -slack_max 1e30 \
                                           -sort_by_slack]

        set pidx 0
        foreach pe $block_paths {
            set path [$pe path]
            foreach pin [$path pins] {
                if {[sta::get_property $pin direction] ne "output"} continue
                set net ""
                catch {set net [get_nets -of_objects $pin]}
                if {$net eq "" || $net eq "NULL"} continue
                set fanout [_net_fanout $net]
                set nname ""
                catch {set nname [get_full_name $net]}
                if {$nname eq ""} { set nname "?" }
                puts $fh "$i\t$pidx\t$nname\t$fanout"
                incr written
            }
            incr pidx
        }

        # Mask the block so the next outer iteration surfaces a different one.
        set_false_path -from $sp_sel -to $ep_sel
    }
    close $fh
    puts "BPN: wrote $written block-path-net rows to $out_path"
}

# Top-N raw critical paths (no block grouping): pull the N worst paths by
# slack via a single find_timing_paths call (one worst path per endpoint),
# decompose each into per-net fanouts, and emit one row per (path_idx, net).
proc find_top_path_net_fanouts {out_path top_n stage spef set_rc top_module} {
    setup_parasitics $stage $spef $set_rc

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    puts $fh "# kind\ttop_path_net_fanout"
    puts $fh "# path_idx\tslack_ns\tnet_name\tfanout"

    # group_path_count caps per path group, so multi-group designs return more
    # than top_n total. -sort_by_slack sorts the combined list globally, so
    # slicing the first top_n gives the true worst-N by slack.
    set paths [find_timing_paths -path_delay max \
                                 -group_path_count $top_n \
                                 -endpoint_path_count 1 \
                                 -slack_max 1e30 \
                                 -sort_by_slack]
    if {[llength $paths] > $top_n} {
        set paths [lrange $paths 0 [expr {$top_n - 1}]]
    }

    set written 0
    set pidx 0
    foreach pe $paths {
        set path [$pe path]
        set slack [sta::get_property $pe slack]
        foreach pin [$path pins] {
            if {[sta::get_property $pin direction] ne "output"} continue
            set net ""
            catch {set net [get_nets -of_objects $pin]}
            if {$net eq "" || $net eq "NULL"} continue
            set fanout [_net_fanout $net]
            set nname ""
            catch {set nname [get_full_name $net]}
            if {$nname eq ""} { set nname "?" }
            puts $fh "$pidx\t[format %.4f $slack]\t$nname\t$fanout"
            incr written
        }
        incr pidx
    }
    close $fh
    puts "TPN: wrote $written top-path-net rows from $pidx paths to $out_path"
}
