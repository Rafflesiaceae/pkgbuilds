#! perl
# A 'simple' script which takes the current "viewport" contents, base64-encodes
# them and calls an "edit-from-base64-arg" script with it as 1st argument.
#
# Requires: community/perl-mime-tools (https://perldoc.perl.org/MIME::Base64)
use MIME::Base64;
use Encode;

sub on_user_command {
   my ($term, $cmd) = @_;

   $cmd eq "open-buffer-in-editor"
      and $term->exec;

   ()
}

sub exec {
   my ($term) = @_;

   my @viewport = ();
   my $line;
   for (my $i = $term->view_start; $i < ($term->view_start + $term->nrow); $i++) {
      $line = $term->ROW_t ($i);
      $line =~ s/\s+$//;  # rstrip
      push(@viewport, $line);
   }
   my $joinedViewport = join "\n", @viewport;
   my $encoded = encode_base64(Encode::encode_utf8($joinedViewport));
   $term->exec_async ("edit-from-base64-arg", $encoded);

   ()
}

# vim:set sw=3 sts=3 et:
