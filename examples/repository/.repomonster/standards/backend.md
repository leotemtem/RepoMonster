# Backend standards

Route handlers must not perform direct database queries. Domain failures must be translated into deliberate API responses without exposing internal error details.
