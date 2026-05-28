# Dump design area and total power as marker-wrapped stdout blocks so the
# Python driver in analyse.py can scrape them into report headers.

puts "ANALYSE_AREA_BEGIN"
report_design_area
puts "ANALYSE_AREA_END"
puts "ANALYSE_POWER_BEGIN"
report_power
puts "ANALYSE_POWER_END"
