# Find the top-N worst *logical* paths from a loaded timing database, where a
# logical path is one n-bit register/IO bus to another -- i.e. all bit-level
# paths between the same (source-bus, dest-bus) pair collapse to one entry.
#
# Strategy: iteratively pull the single worst remaining path, record its
# (start_base -> end_base) pair, then set_false_path that exact pair so the next
# iteration surfaces a new distinct pair. Masking by the pair (not by the
# destination alone) keeps a second source into the same dest bus visible.
#
# Driven by worst_logical_paths.py via env vars:
#   WLP_OUT          -- output report path (required)
#   WLP_TOP_N        -- number of distinct logical paths to emit (default 50)
#   WLP_STAGE        -- stage label for the header (default "")
#   WLP_TOP_MODULE   -- top module name for the header (default "")
#
# No path-exception cleanup is needed: this runs under a one-shot
# `openroad -no_init -exit`, so every set_false_path dies with the process.

if {![info exists env(WLP_OUT)]} {
    error "WLP_OUT env var not set"
}
set out_path $env(WLP_OUT)
set top_n 50
if {[info exists env(WLP_TOP_N)]} { set top_n $env(WLP_TOP_N) }
set stage ""
if {[info exists env(WLP_STAGE)]} { set stage $env(WLP_STAGE) }
set top_module ""
if {[info exists env(WLP_TOP_MODULE)]} { set top_module $env(WLP_TOP_MODULE) }

# name -> the same name with every numeric index "[N]" rewritten to a
# positional placeholder "[I]", "[J]", "[K]", ... in order of appearance, so
# structurally-identical buses across instance arrays collapse to one logical
# path while the original index slots stay visible.
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

# full pin/port name -> logical base: drop the trailing "/<pin>" segment if
# present (registers have one, IO ports do not) and any yosys leaf cell-type
# tag ("...L[0]$_DFF_P_"), then template every index to a placeholder.
proc reg_base {full} {
    set name [regsub {/[^/]+$} $full {}]
    set name [regsub {\$[^/.]*$} $name {}]
    return [index_template $name]
}

# instance/port name -> a glob matching the whole logical bus: every numeric
# index "[N]" widened to "[*]" (any trailing yosys tag is index-free and stays).
proc bus_glob {name} {
    return [regsub -all {\[[0-9]+\]} $name {[*]}]
}

# full pin/port name -> a collection naming the whole bus, for set_false_path.
# Registers are masked by their instance(s) (robust to whether STA reports the
# CK/Q/D pin); IO is masked by port(s).
proc path_selector {full} {
    if {[string match "*/*" $full]} {
        set inst [regsub {/[^/]+$} $full {}]
        return [get_cells [bus_glob $inst]]
    }
    return [get_ports [bus_glob $full]]
}

set fh [open $out_path w]
puts $fh "# stage\t$stage"
puts $fh "# top_module\t$top_module"
puts $fh "# top_n\t$top_n"
puts $fh "# rank\tworst_slack_ns\tstart\tend"

set written 0
for {set i 1} {$i <= $top_n} {incr i} {
    # -sort_by_slack is load-bearing: without it find_timing_paths returns the
    # worst path *per path group* in group order, so lindex 0 would be the worst
    # of whatever group sorts first (e.g. async-reset recovery checks) rather
    # than the global worst.
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

    # Blacklist this exact source-bus -> dest-bus pair for the next iteration.
    set_false_path -from [path_selector $sp] -to [path_selector $ep]
}
close $fh
puts "WLP: wrote $written logical paths to $out_path"
