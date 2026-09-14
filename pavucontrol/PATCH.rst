PATCH: Suspend pavucontrol volume meters while the window is unmapped
========================================================================

Goal
----

Reduce pavucontrol CPU usage while it is running on a non-visible workspace
without permanently disabling its volume meters.

pavucontrol already supports disabling its volume meters via the
"Show volume meters" checkbox. Internally, this works by corking the PulseAudio
peak-monitor streams with ``pa_stream_cork()``.

The desired behavior is:

* While the pavucontrol window is visible/mapped:
  * If "Show volume meters" is enabled, peak-monitor streams should run.
  * If "Show volume meters" is disabled, peak-monitor streams should remain
    corked.
* While the pavucontrol window is hidden/unmapped:
  * All peak-monitor streams should be corked regardless of the checkbox state.
* When the window becomes visible/mapped again:
  * Peak-monitor streams should resume only if "Show volume meters" is enabled.
* Peak-monitor streams created while the window is hidden must start corked.

This is especially useful with window managers such as i3, where windows on
non-visible workspaces are normally unmapped.


Scope
-----

Modify pavucontrol's main window logic only.

Expected files:

* ``src/mainwindow.h``
* ``src/mainwindow.cc``

Do not change the user-facing semantics of the existing "Show volume meters"
checkbox. The checkbox still means whether volume meters are enabled when the
window is visible.


Implementation strategy
-----------------------

1. Add a helper that determines whether volume meters should currently be
   active.

   Suggested interface::

       bool volumeMetersActive() const;

   Suggested semantics::

       return showVolumeMetersCheckButton->get_active() && get_mapped();

2. Add a helper that corks or uncorks every currently existing peak-monitor
   stream.

   Suggested interface::

       void setPeakStreamsCorked(bool corked);

   It must handle the peak streams belonging to all relevant widget maps,
   including:

   * sinks
   * sources
   * sink inputs
   * source outputs

   For each non-null ``pa_stream *``:

   * call ``pa_stream_cork(stream, corked ? 1 : 0, NULL, NULL)``
   * if the returned ``pa_operation *`` is non-null, unref it with
     ``pa_operation_unref()``

   Avoid duplicating this logic in multiple handlers.

3. Override GTK map/unmap handling on the main window.

   Add declarations in ``src/mainwindow.h`` similar to::

       void on_map() override;
       void on_unmap() override;

   Implement them in ``src/mainwindow.cc``.

   Required behavior for ``on_map()``:

   * call the parent implementation
   * if the "Show volume meters" checkbox is enabled, uncork the peak streams

   Suggested shape::

       void MainWindow::on_map() {
           Gtk::Window::on_map();

           if (showVolumeMetersCheckButton->get_active())
               setPeakStreamsCorked(false);
       }

   Required behavior for ``on_unmap()``:

   * cork all peak-monitor streams before or around the parent unmap handling
   * call the parent implementation

   Suggested shape::

       void MainWindow::on_unmap() {
           setPeakStreamsCorked(true);
           Gtk::Window::on_unmap();
       }

4. Ensure newly created peak-monitor streams also respect window visibility.

   Search ``src/mainwindow.cc`` for the places where peak-monitor streams are
   created with flags similar to::

       !showVolumeMetersCheckButton->get_active()
           ? PA_STREAM_START_CORKED
           : PA_STREAM_NOFLAGS

   Replace that decision with the new helper so that streams created while the
   main window is unmapped start corked.

   Desired logic::

       !volumeMetersActive()
           ? PA_STREAM_START_CORKED
           : PA_STREAM_NOFLAGS

   This is important. Corking only the already-existing streams from
   ``on_unmap()`` is insufficient because a new sink/source/application may
   appear while pavucontrol is hidden. Such a newly created peak stream must not
   begin running until the window is visible again.

5. Refactor the existing "Show volume meters" checkbox handler.

   Find::

       MainWindow::onShowVolumeMetersCheckButtonToggled()

   Keep its existing responsibility for updating the widgets' visual meter
   visibility.

   Replace any duplicated per-map ``pa_stream_cork()`` loops with the new
   ``setPeakStreamsCorked()`` helper.

   The effective corking state must depend on BOTH:

   * the checkbox state
   * whether the window is mapped

   Suggested logic::

       bool state = showVolumeMetersCheckButton->get_active();

       setPeakStreamsCorked(!(state && get_mapped()));

   Continue calling ``setVolumeMeterVisible(state)`` on the relevant widgets so
   the checkbox keeps its current user-visible behavior.


Behavioral truth table
----------------------

The resulting behavior should be equivalent to:

=========================  =======================  =========================
Window state               Show volume meters      Peak-monitor streams
=========================  =======================  =========================
mapped                     enabled                  running
mapped                     disabled                 corked
unmapped                   enabled                  corked
unmapped                   disabled                 corked
=========================  =======================  =========================

When moving from unmapped to mapped, streams resume only in the first case.


Important edge cases
--------------------

New audio stream while hidden
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scenario:

1. pavucontrol is visible.
2. User switches to another i3 workspace.
3. ``on_unmap()`` corks all existing peak streams.
4. A new application starts playing audio.
5. pavucontrol receives the new sink-input/source/etc. and creates a new peak
   monitor.

The new peak monitor MUST be created with ``PA_STREAM_START_CORKED``.

This is why the peak-stream creation flags must use ``volumeMetersActive()``
instead of consulting only the checkbox.

Checkbox toggled while hidden
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If the checkbox is changed while the window is technically unmapped, no peak
stream should become active until the window is mapped again.

Using::

    setPeakStreamsCorked(!(state && get_mapped()));

satisfies this requirement.

Window becomes visible again
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the main window is mapped:

* checkbox enabled -> uncork streams
* checkbox disabled -> leave streams corked

Do not blindly uncork all streams from ``on_map()``.


Code quality requirements
-------------------------

Prefer a small refactor over scattering visibility checks throughout the file.

The final implementation should ideally have one central function for corking
all existing peak streams and one function that expresses whether meters should
currently be active.

Avoid introducing timers, polling, i3-specific IPC, workspace detection, or
shell commands. GTK's normal map/unmap lifecycle should be sufficient.

Do not change PulseAudio/PipeWire server configuration.

Do not remove the existing "Show volume meters" feature.


Build
-----

Use the project's normal Meson build.

Typical workflow::

    meson setup build
    meson compile -C build

Run the locally built binary, typically::

    ./build/src/pavucontrol

If the build directory already exists, do not recreate it unnecessarily.


Testing
-------

Functional test
~~~~~~~~~~~~~~~

1. Start the patched pavucontrol.
2. Enable "Show volume meters".
3. Play audio and confirm meters update.
4. Switch to another i3 workspace.
5. Confirm pavucontrol remains running.
6. Switch back.
7. Confirm the meters resume normally.

Repeat with "Show volume meters" disabled and confirm they never resume.

New-stream test
~~~~~~~~~~~~~~~

1. Open pavucontrol with meters enabled.
2. Switch away from its workspace.
3. While it is hidden, start a previously inactive audio application.
4. Verify pavucontrol does not begin consuming significant CPU due to the new
   peak stream.
5. Switch back to pavucontrol.
6. Verify the newly created stream's meter begins updating normally.

CPU test
~~~~~~~~

Measure pavucontrol CPU usage while visible and while hidden.

For example::

    pidstat -p "$(pgrep -n pavucontrol)" 1

or::

    top -p "$(pgrep -n pavucontrol)"

Expected result:

* visible with active meters -> normal meter-related CPU activity
* hidden on another workspace -> CPU use should fall substantially toward idle

Regression checks
~~~~~~~~~~~~~~~~~

Verify:

* sinks still appear/disappear correctly
* sources still appear/disappear correctly
* sink inputs still appear/disappear correctly
* source outputs still appear/disappear correctly
* volume sliders continue to work
* muting continues to work
* switching tabs does not break meters
* toggling "Show volume meters" still works
* hiding/showing the application repeatedly does not crash
* no obvious PulseAudio/PipeWire warnings appear in stderr


Acceptance criteria
-------------------

The patch is complete when all of the following are true:

* Existing peak-monitor streams are corked whenever the main window is unmapped.
* Existing peak-monitor streams are uncorked on map only when the checkbox is
  enabled.
* Newly created peak-monitor streams start corked while the window is unmapped.
* The existing checkbox still controls whether meters are shown while visible.
* pavucontrol's hidden-workspace CPU usage is substantially reduced.
* The patch does not contain i3-specific logic.
* The project builds cleanly with its normal Meson build.


Optional upstream-quality cleanup
---------------------------------

If the surrounding code contains repeated loops over ``sinkWidgets``,
``sourceWidgets``, ``sinkInputWidgets``, and ``sourceOutputWidgets`` solely for
corking peak streams, consolidate those loops in ``setPeakStreamsCorked()``.

Do not perform unrelated refactors.


Suggested commit message
------------------------

::

    mainwindow: suspend peak monitors while unmapped

    Cork PulseAudio peak-monitor streams while the main pavucontrol window is
    unmapped and resume them when it becomes visible again.

    This avoids continuously processing volume-meter updates while pavucontrol
    is hidden, such as when it resides on a non-visible i3 workspace.

    Also start newly created peak-monitor streams corked while the window is
    unmapped.
