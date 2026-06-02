# Find the top-N worst *logical* paths from a loaded timing database, where a
# logical path is one n-bit register/IO bus to another -- i.e. all bit-level
# paths between the same (source-bus, dest-bus) pair collapse to one entry.
#
# Strategy: iteratively pull the single worst remaining path, record its
# (start_base -> end_base) pair, then set_false_path that exact pair so the next
# iteration surfaces a new distinct pair. Masking by the pair (not by the
# destination alone) keeps a second source into the same dest bus visible.
#
# Source this file into an openroad session that already has the db/sdc/liberty
# loaded, then call:
#   find_worst_logical_paths <out_path> <top_n> <stage> <spef> <set_rc> <top_module>
# It establishes the interconnect parasitics for the stage (the .odb carries
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

# name -> the same name with every numeric index "[N]" rewritten to a
# positional placeholder "[I]", "[J]", "[K]".
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

# full pin/port name -> logical base: drop the trailing "/<pin>" segment.
proc reg_base {full} {
    set name [regsub {/[^/]+$} $full {}]
    set name [regsub {\$[^/.]*$} $name {}]
    return [index_template $name]
}

# instance/port name -> a glob matching the whole logical bus.
proc bus_glob {name} {
    return [regsub -all {\[[0-9]+\]} $name {[*]}]
}

# full pin/port name -> a collection naming the whole bus.
proc path_selector {full} {
    if {[string match "*/*" $full]} {
        set inst [regsub {/[^/]+$} $full {}]
        return [get_cells [bus_glob $inst]]
    }
    return [get_ports [bus_glob $full]]
}

# Emit the top `top_n` distinct worst logical paths to `out_path`.
proc find_worst_logical_paths {out_path top_n stage spef set_rc top_module} {
    setup_parasitics $stage $spef $set_rc

    set fh [open $out_path w]
    puts $fh "# stage\t$stage"
    puts $fh "# top_module\t$top_module"
    puts $fh "# top_n\t$top_n"
    puts $fh "# rank\tworst_slack_ns\tstart\tend"

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

        puts $fh "$i\t[format %.4f $slack]\t[reg_base $sp]\t[reg_base $ep]"
        incr written

        # Blacklist this exact source-bus -> dest-bus pair for the next round.
        set_false_path -from [path_selector $sp] -to [path_selector $ep]
    }
    close $fh
    puts "WLP: wrote $written logical paths to $out_path"
}
