# Enrich the GR-emitted routing-congestion report with per-tile
# instance/IO-pin lookups and dump it as a tab-separated table for the
# Python side to ingest.
#
# Sourced after extract_critical_paths.tcl in the same openroad
# invocation; reuses the already-loaded post-route block. No GR
# re-run -- ORFS's global_route step already produced the input rpt
# (see flow/scripts/global_route.tcl). When the design routed clean,
# ORFS writes no rpt at all; this script writes a header-only TSV
# and the Python side treats it as zero overflow.
#
# Env vars:
#   ANALYSE_CONGEST_RPT  -- input GR congestion rpt (optional)
#   ANALYSE_CONGEST_TSV  -- output enriched TSV (required)

if { ![info exists env(ANALYSE_CONGEST_TSV)] } {
    error "ANALYSE_CONGEST_TSV env var not set"
}
set congest_tsv $env(ANALYSE_CONGEST_TSV)
set congest_fh [open $congest_tsv w]
puts $congest_fh "# units: um"
puts $congest_fh "# direction\toverflow\tcapacity\tusage\tlayer\txL\tyL\txH\tyH\tnets\tinsts\tio_pins"

set congest_rpt ""
if { [info exists env(ANALYSE_CONGEST_RPT)] } {
    set congest_rpt $env(ANALYSE_CONGEST_RPT)
}
if { $congest_rpt eq "" || ![file isfile $congest_rpt] } {
    puts $congest_fh "# no congestion report found"
    close $congest_fh
    puts "ANALYSE: no congestion report; wrote header-only TSV to $congest_tsv"
} else {
    set congest_block [ord::get_db_block]

    # Walk the GR report as a small state machine. Each record is four
    # non-blank lines (violation type / srcs / comment / bbox); a new
    # "violation type:" resets state, the "bbox" line emits a row.
    set rfh [open $congest_rpt r]
    set in_rec 0
    set rec_dir ""
    set rec_srcs [list]
    set rec_cap 0
    set rec_usage 0
    set rec_ovfl 0
    set rec_count 0
    while { [gets $rfh line] >= 0 } {
        set t [string trim $line]
        if { $t eq "" } { continue }
        if { [regexp {^violation type:\s+(.+)$} $t -> kind] } {
            set in_rec 1
            set rec_dir "?"
            if { [string match -nocase "Horizontal*" $kind] } { set rec_dir "H" }
            if { [string match -nocase "Vertical*"   $kind] } { set rec_dir "V" }
            set rec_srcs [list]
            continue
        }
        if { !$in_rec } { continue }
        if { [regexp {^srcs:\s*(.*)$} $t -> rest] } {
            set rec_srcs [split $rest]
            continue
        }
        if { [regexp {^comment:\s*capacity:(-?\d+)\s+usage:(-?\d+)\s+overflow:(-?\d+)} \
                  $t -> c u o] } {
            set rec_cap $c
            set rec_usage $u
            set rec_ovfl $o
            continue
        }
        if { [regexp \
                {^bbox\s*=\s*\(([^,]+),\s*([^\)]+)\)\s*-\s*\(([^,]+),\s*([^\)]+)\)\s+on\s+Layer\s+(\S+)} \
                $t -> ax ay bx by lyr] } {
            set xL [string trim $ax]
            set yL [string trim $ay]
            set xH [string trim $bx]
            set yH [string trim $by]
            set insts [list]
            set io_pins [list]
            set nets [list]
            foreach tok $rec_srcs {
                if { [string first "net:" $tok] != 0 } { continue }
                set nname [string range $tok 4 end]
                lappend nets $nname
                set net [$congest_block findNet $nname]
                if { $net eq "NULL" } { continue }
                foreach it [$net getITerms] {
                    set inst [$it getInst]
                    if { $inst ne "NULL" } {
                        # ODB preserves yosys's Verilog escape syntax
                        # (`name\[N\]`); strip the backslashes so the
                        # name matches OpenSTA's path output and the
                        # `_clean_instance` regex in analyse.py.
                        lappend insts [string map [list "\\" ""] [$inst getName]]
                    }
                }
                foreach bt [$net getBTerms] {
                    lappend io_pins "IO:[$bt getName]"
                }
            }
            set nets [lsort -unique $nets]
            set insts [lsort -unique $insts]
            set io_pins [lsort -unique $io_pins]
            puts $congest_fh \
                "$rec_dir\t$rec_ovfl\t$rec_cap\t$rec_usage\t$lyr\t$xL\t$yL\t$xH\t$yH\t[join $nets |]\t[join $insts |]\t[join $io_pins |]"
            incr rec_count
            set in_rec 0
        }
    }
    close $rfh
    close $congest_fh
    puts "ANALYSE: wrote $rec_count overflowing tiles to $congest_tsv (from $congest_rpt)"
}
