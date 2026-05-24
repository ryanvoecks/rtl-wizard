#!/usr/bin/env bash
# UberDDR3's bundled Icarus flow drives ddr3_top (controller+PHY+Micron model).
# The PHY's UNISIM primitives (OSERDESE2/IDELAYE2/IOBUF*/...) are stubbed by
# behavioral models in testbench/models/ when -DSIM_MODEL is set. We synthesize
# only the controller; the PHY+models exist solely for the TB.
#
# Built-in BIST (BIST_MODE=1) issues 4608 writes + 4608 reads across burst,
# random, and alternating r/w patterns. The TB deliberately injects 4 bit
# errors after each pattern to exercise the error-detection path; the pass
# criterion is therefore (Number of Fails == Number of Injected Errors).
set -eo pipefail
cd testbench
iverilog -g2012 -o uberddr3_sim \
    -DNO_TEST_MODEL \
    -DSIM_MODEL \
    -s ddr3_dimm_micron_sim \
    -I . \
    ddr3_dimm_micron_sim.sv \
    ddr3.sv \
    models/IDELAYCTRL_model.v \
    models/IDELAYE2_model.v \
    models/IOBUF_DCIEN_model.v \
    models/IOBUF_model.v \
    models/IOBUFDS_DCIEN_model.v \
    models/IOBUFDS_model.v \
    models/ISERDESE2_model.v \
    models/OBUFDS_model.v \
    models/ODELAYE2_model.v \
    models/OSERDESE2_model.v \
    models/OBUF_model.v \
    ../rtl/ddr3_top.v \
    ../rtl/ddr3_controller.v \
    ../rtl/ddr3_phy.v \
    ddr3_module.sv
vvp -n ./uberddr3_sim | tee uberddr3_sim.log
awk '
/^Number of Writes/            { writes=$NF }
/^Number of Reads/             { reads=$NF }
/^Number of Success/           { success=$NF }
/^Number of Fails/             { fails=$NF }
/^Number of Injected Errors/   { injected=$NF }
END {
  if (writes == "" || reads == "" || success == "" || fails == "" || injected == "") {
    print "UBERDDR3_TEST_FAILED: summary line missing from sim log"
    exit 1
  }
  if (writes+0 == 0 || reads+0 == 0) {
    print "UBERDDR3_TEST_FAILED: zero writes/reads"
    exit 1
  }
  if (fails+0 != injected+0) {
    printf "UBERDDR3_TEST_FAILED: fails=%s != injected=%s\n", fails, injected
    exit 1
  }
  printf "UBERDDR3_TEST_PASSED: writes=%s reads=%s success=%s injected=%s\n", \
    writes, reads, success, injected
}' uberddr3_sim.log
