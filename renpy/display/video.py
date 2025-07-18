# Copyright 2004-2025 Tom Rothamel <pytom@bishoujo.us>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

from __future__ import division, absolute_import, with_statement, print_function, unicode_literals
from renpy.compat import PY2, basestring, bchr, bord, chr, open, pystr, range, round, str, tobytes, unicode  # *

import math
import collections
import re

import renpy

# The movie displayable that's currently being shown on the screen.
current_movie = None

# True if the movie that is currently displaying is in fullscreen mode,
# False if it's a smaller size.
fullscreen = False

# The size of a Movie object that hasn't had an explicit size set.
default_size = (400, 300)

# The file we allocated the surface for.
surface_file = None

# The surface to display the movie on, if not fullscreen.
surface = None


def movie_stop(clear=True, only_fullscreen=False):
    """
    Stops the currently playing movie.
    """

    if (not fullscreen) and only_fullscreen:
        return

    renpy.audio.music.stop(channel="movie")


def movie_start(filename, size=None, loops=0):
    """
    This starts a movie playing.
    """

    if renpy.game.less_updates:
        return

    global default_size

    if size is not None:
        default_size = size

    filename = [filename]

    if loops == -1:
        loop = True
    else:
        loop = False
        filename = filename * (loops + 1)

    renpy.audio.music.play(filename, channel="movie", loop=loop)


def movie_start_fullscreen(filename, size=None, loops=0):
    """
    Starts a movie playing in fullscreen mode. This handles oversampling for
    fullscreen movies.
    """

    if isinstance(filename, str):
        filename = find_oversampled_filename(filename)

    movie_start(filename, size, loops)


movie_start_displayable = movie_start


# A map from a channel name to the movie texture that is being displayed
# on that channel.
texture = {}

# The set of channels that are being displayed in Movie objects.
displayable_channels = collections.defaultdict(list)

# A map from a channel to the topmost Movie being displayed on
# that channel. (Or None if no such movie exists.)
channel_movie = {}

# Is there a video being displayed fullscreen?
fullscreen = False

# Movie channels that had a hide operation since the last interaction took
# place.
reset_channels = set()

# These store the textures for movies in the same group.
group_texture = {}


def early_interact():
    """
    Called early in the interact process, to clear out the fullscreen
    flag.
    """

    displayable_channels.clear()
    channel_movie.clear()


def interact():
    """
    This is called each time the screen is drawn, and should return True
    if the movie should display fullscreen.
    """

    global fullscreen

    for i in list(texture.keys()):
        if not renpy.audio.music.get_playing(i):
            del texture[i]

    if renpy.audio.music.get_playing("movie"):
        for i in displayable_channels.keys():
            if i[0] == "movie":
                fullscreen = False
                break
        else:
            fullscreen = True

    else:
        fullscreen = False

    return fullscreen


def get_movie_texture(channel, mask_channel=None, side_mask=False, mipmap=None):
    if not renpy.audio.music.get_playing(channel):
        return None, False

    if mipmap is None:
        mipmap = renpy.config.mipmap_movies

    if renpy.emscripten:
        # Use an optimized function for web
        return get_movie_texture_web(channel, mask_channel, side_mask, mipmap)

    c = renpy.audio.music.get_channel(channel)
    surf = c.read_video()

    if side_mask:
        if surf is not None:
            w, h = surf.get_size()
            w //= 2

            mask_surf = surf.subsurface((w, 0, w, h))
            surf = surf.subsurface((0, 0, w, h))

        else:
            mask_surf = None

    elif mask_channel:
        mc = renpy.audio.music.get_channel(mask_channel)
        mask_surf = mc.read_video()
    else:
        mask_surf = None

    if mask_surf is not None:
        # Something went wrong with the mask video.
        if surf:
            renpy.display.module.alpha_munge(mask_surf, surf, renpy.display.im.identity)
        else:
            surf = None

    if surf is not None:
        renpy.display.render.mutated_surface(surf)
        tex = renpy.display.draw.load_texture(surf, True, {"mipmap": mipmap})
        texture[channel] = tex
        new = True
    else:
        tex = texture.get(channel, None)
        new = False

    return tex, new


def get_movie_texture_web(channel, mask_channel, side_mask, mipmap):
    """
    This method returns either a GLTexture or a Render.
    """
    c = renpy.audio.music.get_channel(channel)
    # read_video() returns a GLTexture for web
    tex = c.read_video()

    if side_mask:
        if tex is not None:
            w, h = tex.get_size()
            w //= 2

            mask_tex = tex.subsurface((w, 0, w, h))
            tex = tex.subsurface((0, 0, w, h))

        else:
            mask_tex = None

    elif mask_channel:
        mc = renpy.audio.music.get_channel(mask_channel)
        mask_tex = mc.read_video()
    else:
        mask_tex = None

    if mask_tex is not None:
        # Something went wrong with the mask video.
        if tex:
            # Apply alpha using mask
            rv = renpy.display.render.Render(*tex.get_size())
            rv.blit(tex, (0, 0))
            rv.blit(mask_tex, (0, 0))

            rv.mesh = True
            rv.add_shader("renpy.alpha_mask")

            # Not a texture anymore
            tex = rv

        else:
            tex = None

    if tex is not None:
        texture[channel] = tex
        new = True
    else:
        tex = texture.get(channel, None)
        new = False

    return tex, new


def resize_movie(r, width, height):
    """
    A utility function to resize a Render or texture to the given
    dimensions.
    """

    if r is None:
        return None

    rv = renpy.display.render.Render(width, height)

    sw, sh = r.get_size()

    if not (sw and sh):
        return rv

    scale = min(1.0 * width / sw, 1.0 * height / sh)  # type: float

    dw = scale * sw
    dh = scale * sh

    rv.forward = renpy.display.matrix.Matrix2D(1.0 / scale, 0.0, 0.0, 1.0 / scale)
    rv.reverse = renpy.display.matrix.Matrix2D(scale, 0.0, 0.0, scale)
    rv.blit(r, (int((width - dw) / 2), int((height - dh) / 2)))

    return rv


def render_movie(channel, width, height):
    """
    Called from the Draw objects to render and scale a fullscreen movie.
    """

    tex, _new = get_movie_texture(channel)
    return resize_movie(tex, width, height)


def find_oversampled_filename(filename):
    """
    When automatic oversampling is enabled, this function will search the filename for an
    oversampled movie.
    """

    if (
        "@" not in filename
        and renpy.config.automatic_oversampling
        and renpy.display.draw
        and renpy.display.draw.draw_per_virt > 1.0
    ):
        max_oversample = 2 ** int(math.ceil(math.log2(renpy.display.draw.draw_per_virt)))
        max_oversample = min(max_oversample, renpy.config.automatic_oversampling)

        base, _, ext = filename.rpartition(".")
        base, _, extras = base.partition("@")

        if extras:
            extras = "," + extras

        for i in range(max_oversample, 1, -1):
            new_filename = f"{base}@{i}{extras}.{ext}"

            if Movie.any_loadable(new_filename):
                filename = new_filename
                break

    return filename


def find_oversampled(new, filename):
    """
    This is used by default_play_callback to find the oversampled version of a video, to
    """

    if new.oversample is not None:
        oversample = 1.0 * new.oversample
    elif not isinstance(filename, str):
        oversample = 1.0
    else:
        filename = find_oversampled_filename(filename)

        oversample = 1.0

        if "@" in filename:
            if filename[0] == "<":
                filename = filename.partition(">")[2]

            base = filename.rpartition(".")[0]
            extras = base.rpartition("@")[2].partition("/")[0].split(",")

            for i in extras:
                try:
                    oversample = float(i)
                except Exception:
                    raise Exception("Unknown movie modifier %r in %r." % (i, filename))

    new.playing_oversample = oversample
    return filename


def default_play_callback(old, new):  # @UnusedVariable
    if new.mask:
        renpy.audio.music.play(find_oversampled(new, new.mask), channel=new.mask_channel, loop=new.loop)

    renpy.audio.music.play(find_oversampled(new, new._play), channel=new.channel, loop=new.loop)


# A serial number that's used to generated movie channels.
movie_channel_serial = 0


class Movie(renpy.display.displayable.Displayable):
    """
    :doc: movie
    :args: (*, size=None, channel="movie", play=None, side_mask=False, mask=None, mask_channel=None, start_image=None, image=None, play_callback=None, loop=True, group=None, **properties)

    This is a displayable that shows the current movie.

    `size`
        This should be specified as either a tuple giving the width and
        height of the movie, or None to automatically adjust to the size
        of the playing movie. (If None, the displayable will be (0, 0)
        when the movie is not playing.)

    `channel`
        The audio channel associated with this movie. When a movie file
        is played on that channel, it will be displayed in this Movie
        displayable. If this is left at the default of "movie", and `play`
        is provided, a channel name is automatically selected, using
        :var:`config.single_movie_channel` and :var:`config.auto_movie_channel`.

    `play`
        If given, this should be the path to a movie file, or a list
        of paths to movie files. These movie
        files will be automatically played on `channel` when the Movie is
        shown, and automatically stopped when the movie is hidden.

    `side_mask`
        If true, this tells Ren'Py to use the side-by-side mask mode for
        the Movie. In this case, the movie is divided in half. The left
        half is used for color information, while the right half is used
        for alpha information. The width of the displayable is half the
        width of the movie file.

        Where possible, `side_mask` should be used over `mask` as it has
        no chance of frames going out of sync.

    `mask`
        If given, this should be the path to a movie file, or a list of paths
        to movie files, that are used as
        the alpha channel of this displayable. The movie file will be
        automatically played on `movie_channel` when the Movie is shown,
        and automatically stopped when the movie is hidden.

    `mask_channel`
        The channel the alpha mask video is played on. If not given,
        defaults to `channel`\\_mask. (For example, if `channel` is "sprite",
        `mask_channel` defaults to "sprite_mask".)

    `start_image`
        An image that is displayed when playback has started, but the
        first frame has not yet been decoded.

    `image`
        An image that is displayed when `play` has been given, but the
        file it refers to does not exist. (For example, this can be used
        to create a slimmed-down mobile version that does not use movie
        sprites.) Users can also choose to fall back to this image as a
        preference if video is too taxing for their system. The image will
        also be used if the video plays, and then the movie ends, unless
        `group` is given.

    `play_callback`
        If not None, a function that's used to start the movies playing.
        (This may do things like queue a transition between sprites, if
        desired.) It's called with the following arguments:

        `old`
            The old Movie object, or None if the movie is not playing.
        `new`
            The new Movie object.

        A movie object has the `play` parameter available as ``_play``,
        while the ``channel``, ``loop``, ``mask``, and ``mask_channel`` fields
        correspond to the given parameters.

        Generally, this will want to use :func:`renpy.music.play` to start
        the movie playing on the given channel, with synchro_start=True.
        A minimal implementation is::

            def play_callback(old, new):

                renpy.music.play(new._play, channel=new.channel, loop=new.loop, synchro_start=True)

                if new.mask:
                    renpy.music.play(new.mask, channel=new.mask_channel, loop=new.loop, synchro_start=True)

    `loop`
        If False, the movie will not loop. If `image` is defined, the image
        will be displayed when the movie ends. Otherwise, the displayable will
        become transparent.

    `group`
        If not None, this should be a string. If given, and if the movie has not
        yet started playing, and another movie in the same group has played in
        the previous frame, the last frame from that movie will be used for
        this movie. This can prevent flashes of transparency when switching
        between two movies.

    `keep_last_frame`
        If true, and the movie has ended, the last frame will be displayed,
        rather than the movie being hidden. This only works if `loop` is
        false. (This behavior will also occur if `group` is set.)

    `oversample`
        If this is greater than 1, the movieis considered to be oversampled,
        with more pixels than its logical size would imply. For example, if
        an movie file is 1280x720 and oversample is 2, then the image will
        be treated as a 640x360 movie for the purpose of layout.

        If None, Ren'Py will automatically determine oversamping. If an @ followed
        by a number is found in the filename, that number will be used as the the
        oversampling factor. Otherwise, Ren'Py will search for files and use those.

        Specifically, if :file`launch.webm` is used, Ren'Py will search for :file:`launch@2.webm`
        if the movie is scaled up more than 1x, and :file:`launch@4.webm` and :file:`launch@3.webm`
        if the movie is scaled up more than 2x.

        Automatic oversampling of movies only happens when the movie begins playing.
    """

    fullscreen = False
    channel = "movie"
    _play = None
    _original_play = None

    mask = None
    mask_channel = None
    side_mask = False

    image = None
    start_image = None

    play_callback = None

    loop = True
    group = None

    oversample: float | None = None
    """The oversampling factor of the movie given in Movie.__init__"""

    playing_oversample: float = 1
    """The oversampling factor of the movie that is currently playing."""

    @staticmethod
    def any_loadable(name):
        """
        If `name` is a string, checks if that filename is loadable.
        If `name` is a list of strings, checks if any filenames is loadable.
        """

        if isinstance(name, str):
            m = re.match(r"<.*>(.*)$", name)
            if m:
                name = m.group(1)
            return renpy.loader.loadable(name, directory="audio")
        else:
            return any(renpy.loader.loadable(i, directory="audio") for i in name)

    def after_setstate(self):
        play = self._original_play or self._play
        if (play is not None) and self.any_loadable(play):
            self._original_play = self._play = play
        else:
            self._play = None
            self._original_play = play

        global movie_channel_serial

        if (self.channel is not None) and ((" " in self.channel) or ("/" in self.channel)):
            self.channel = "_movie_{}".format(movie_channel_serial)
            movie_channel_serial += 1

            if self.mask_channel is not None:
                self.mask_channel = self.channel + "_mask"

    def ensure_channel(self, name):
        if name is None:
            return

        if renpy.audio.music.channel_defined(name):
            return

        if self.mask:
            framedrop = True
        else:
            framedrop = False

        renpy.audio.music.register_channel(
            name, renpy.config.movie_mixer, loop=True, stop_on_mute=False, movie=True, framedrop=framedrop, force=True
        )

    def ensure_channels(self):
        self.ensure_channel(self.channel)
        self.ensure_channel(self.mask_channel)

    keep_last_frame_serial = 0

    def __init__(
        self,
        fps=24,
        size=None,
        channel="movie",
        play=None,
        mask=None,
        mask_channel=None,
        image=None,
        play_callback=None,
        side_mask=False,
        loop=True,
        start_image=None,
        group=None,
        keep_last_frame=False,
        oversample=None,
        **properties,
    ):
        global movie_channel_serial

        super(Movie, self).__init__(**properties)

        if channel == "movie" and play and renpy.config.single_movie_channel:
            channel = renpy.config.single_movie_channel
        elif channel == "movie" and play and renpy.config.auto_movie_channel:
            channel = "_movie_{}".format(movie_channel_serial)
            movie_channel_serial += 1

        self.size = size
        self.channel = channel
        self.loop = loop

        self._original_play = play
        if (play is not None) and self.any_loadable(play):
            self._play = play

        if side_mask:
            mask = None

        self.mask = mask

        if mask is None:
            self.mask_channel = None
        elif mask_channel is None:
            self.mask_channel = channel + "_mask"
        else:
            self.mask_channel = mask_channel

        self.side_mask = side_mask

        self.ensure_channels()

        self.image = renpy.easy.displayable_or_none(image)
        self.start_image = renpy.easy.displayable_or_none(start_image)

        self.play_callback = play_callback

        if group is None and keep_last_frame:
            group = "_keep_last_frame_" + str(Movie.keep_last_frame_serial)
            Movie.keep_last_frame_serial += 1

        self.group = group

        if self.image and self.image._duplicatable:
            self._duplicatable = True

        if self.start_image and self.start_image._duplicatable:
            self._duplicatable = True

    def _duplicate(self, args):
        if not self._duplicatable:
            return self

        rv = self._copy(args)

        if rv.image and rv.image._duplicatable:
            rv.image = rv.image._duplicate(args)

        if rv.start_image and rv.start_image._duplicatable:
            rv.start_image = rv.start_image._duplicate(args)

        return rv

    def _handles_event(self, event):
        return event == "show"

    def set_transform_event(self, event):
        if event == "show":
            reset_channels.add(self.channel)

    def render(self, width, height, st, at):
        self.ensure_channels()

        if self._play and not (renpy.game.preferences.video_image_fallback is True):
            if channel_movie.get(self.channel, None) is not self:
                channel_movie[self.channel] = self

        playing = renpy.audio.music.get_playing(self.channel)

        not_playing = not playing

        if self.channel in reset_channels:
            not_playing = False

        if self.group is not None and self.group in group_texture:
            not_playing = False

        if (self.image is not None) and not_playing:
            surf = renpy.display.render.render(self.image, width, height, st, at)
            w, h = surf.get_size()
            rv = renpy.display.render.Render(w, h)
            rv.blit(surf, (0, 0))

            return rv

        tex, _ = get_movie_texture(self.channel, self.mask_channel, self.side_mask, self.style.mipmap)

        if self.group is not None:
            if tex is None:
                tex = group_texture.get(self.group, None)
            else:
                group_texture[self.group] = tex

        if (not not_playing) and (tex is not None):
            width, height = tex.get_size()

            rv = renpy.display.render.Render(width, height)
            rv.blit(tex, (0, 0))

            if self.playing_oversample != 1:
                rv.reverse = renpy.display.matrix.Matrix2D(
                    1.0 / self.playing_oversample, 0.0, 0.0, 1.0 / self.playing_oversample
                )
                rv.forward = renpy.display.matrix.Matrix2D(self.playing_oversample, 0.0, 0.0, self.playing_oversample)

        elif (not not_playing) and (self.start_image is not None):
            surf = renpy.display.render.render(self.start_image, width, height, st, at)
            w, h = surf.get_size()
            rv = renpy.display.render.Render(w, h)
            rv.blit(surf, (0, 0))

        else:
            rv = renpy.display.render.Render(0, 0)

        if self.size is not None:
            rv = resize_movie(rv, self.size[0], self.size[1])

        # Usually we get redrawn when the frame is ready - but we want
        # the movie to disappear if it's ended, or if it hasn't started
        # yet.
        renpy.display.render.redraw(self, 0.1)

        return rv

    def play(self, old):
        self.ensure_channels()

        if old is None:
            old_play = None
        else:
            old_play = old._play

        if (self._play is None) and (old_play is None):
            return

        if (self._play != old_play) or renpy.config.replay_movie_sprites:
            if self._play:
                if self.play_callback is not None:
                    self.play_callback(old, self)
                else:
                    default_play_callback(old, self)

            else:
                renpy.audio.music.stop(channel=self.channel, fadeout=0)

                if self.mask:
                    renpy.audio.music.stop(channel=self.mask_channel, fadeout=0)  # type: ignore

    def stop(self):
        self.ensure_channels()

        if self._play:
            if renpy.audio.music.channel_defined(self.channel):
                renpy.audio.music.stop(channel=self.channel, fadeout=0)

            if self.mask:
                if renpy.audio.music.channel_defined(self.mask_channel):
                    renpy.audio.music.stop(channel=self.mask_channel, fadeout=0)  # type: ignore

    def per_interact(self):
        self.ensure_channels()

        displayable_channels[(self.channel, self.mask_channel)].append(self)
        renpy.display.render.redraw(self, 0)

    def visit(self):
        return [self.image, self.start_image]


def playing():
    if renpy.audio.music.get_playing("movie"):
        return True

    for i in displayable_channels:
        channel, _mask_channel = i

        if renpy.audio.music.get_playing(channel):
            return True

    return


# A map from a channel to the movie playing on it in the last
# interaction. Used to restart looping movies.
last_channel_movie = {}


def update_playing():
    """
    Calls play/stop on Movie displayables.
    """

    global last_channel_movie

    old_channel_movie = renpy.game.context().movie

    for c, m in channel_movie.items():
        old = old_channel_movie.get(c, None)
        last = last_channel_movie.get(c, None)

        if (c in reset_channels) and renpy.config.replay_movie_sprites:
            m.play(old)
        elif old is m or last is m:
            continue
        elif old is not m:
            m.play(old)
        elif m.loop and last is not m:
            m.play(last)

    stopped = set()

    for c, m in last_channel_movie.items():
        if c not in channel_movie:
            stopped.add(c)
            m.stop()

    for c, m in old_channel_movie.items():
        if c not in channel_movie:
            if c not in stopped:
                m.stop()

    renpy.game.context().movie = last_channel_movie = dict(channel_movie)
    reset_channels.clear()


def frequent():
    """
    Called to update the video playback. Returns true if a video refresh is
    needed, false otherwise.
    """

    update_playing()

    renpy.audio.audio.advance_time()

    # Cycle the group textures.
    global group_texture

    old_group_texture = group_texture
    group_texture = {}

    for movies in displayable_channels.values():
        for m in movies:
            if m.group is not None:
                group_texture[m.group] = old_group_texture.get(m.group, None)

    if fullscreen:
        c = renpy.audio.audio.get_channel("movie")

        if c.video_ready():
            return True
        else:
            return False

    # Determine if we need to redraw.
    elif displayable_channels:
        update = True

        for i in displayable_channels:
            channel, mask_channel = i

            c = renpy.audio.audio.get_channel(channel)
            if not c.video_ready():
                update = False
                break

            if mask_channel:
                c = renpy.audio.audio.get_channel(mask_channel)
                if not c.video_ready():
                    update = False
                    break

        if update:
            for v in displayable_channels.values():
                for j in v:
                    renpy.display.render.redraw(j, 0.0)

        return False

    return False
