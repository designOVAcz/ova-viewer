# The launcher's loader line - added to PyInstaller's splash script by the
# spec (see Ova Viewer.spec). It runs in the exe's own splash thread, so it
# keeps moving whatever Python is doing: on a cold start (after a reboot,
# Defender scanning, fonts read from disk) Python loads for 10-20 s, and the
# line stood still meanwhile - "is it doing anything?".
#
# 1 px near white, the globe's equator width, centred on the screen. It grows
# from where it starts quickly, then ever slower, never quite to the end:
#     p(t) = START + (1 - START - LEFT) * (1 - exp(-t / TAU))
# When the app's splash is ready, Python sets status_text to "ova:done"
# (pyi_splash.update_text): the line runs to the end, then the app's own
# splash takes over with the same line, full (see random_image_viewer/app.py).
#
# The exe is DPI-aware from the start (its manifest, see the spec), so sizes
# here are real pixels: the line is scaled by the screen's DPI. The image is
# just a transparent box bigger than any scaled line (tools/make_splash.py).

set ova_line_w 275          ;# the line, at 100 % (= splash.py: the equator's width)
set ova_start 0.06
set ova_left 0.08           ;# what is never reached before "ova:done"
set ova_tau 5.0             ;# s

set ova_t0 [clock milliseconds]
set ova_scale [expr {[winfo fpixels . 1i] / 96.0}]
if {$ova_scale < 1.0} {set ova_scale 1.0}
set ova_len [expr {$ova_line_w * $ova_scale}]
if {$ova_len > $image_width - 2} {set ova_len [expr {$image_width - 2}]}
set ova_x0 [expr {round(($image_width - $ova_len) / 2.0)}]
set ova_y [expr {$image_height / 2}]
set ova_p $ova_start
set ova_done 0
set ova_done_at 0
set ova_done_from 0.0

.root.canvas create rectangle $ova_x0 $ova_y $ova_x0 [expr {$ova_y + 1}] \
    -fill #eef0f2 -outline "" -width 0 -tags ovaline

proc ova_tick {} {
    global ova_t0 ova_start ova_left ova_tau ova_len ova_x0 ova_y ova_p
    global ova_done ova_done_at ova_done_from
    set now [clock milliseconds]
    if {$ova_done} {
        set k [expr {($now - $ova_done_at) / 180.0}]
        if {$k > 1.0} {set k 1.0}
        set k [expr {$k * $k * (3.0 - 2.0 * $k)}]
        set p [expr {$ova_done_from + (1.0 - $ova_done_from) * $k}]
    } else {
        set t [expr {($now - $ova_t0) / 1000.0}]
        set p [expr {$ova_start + (1.0 - $ova_start - $ova_left) * (1.0 - exp(-$t / $ova_tau))}]
    }
    set ova_p $p
    set x1 [expr {round($ova_x0 + $ova_len * $p)}]
    .root.canvas coords ovaline $ova_x0 $ova_y $x1 [expr {$ova_y + 1}]
    after 16 ova_tick
}

proc ova_status {args} {
    global status_text ova_done ova_done_at ova_done_from ova_p
    if {[info exists status_text] && $status_text eq "ova:done" && !$ova_done} {
        set ova_done 1
        set ova_done_at [clock milliseconds]
        set ova_done_from $ova_p
    }
}

if {![info exists status_text]} {set status_text ""}
trace add variable status_text write ova_status
ova_tick
