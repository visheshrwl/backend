require_relative 'lib/labkit/version'

Gem::Specification.new do |spec|
  spec.name          = 'labkit'
  spec.version       = Labkit::VERSION
  spec.summary       = 'Zero-setup Postgres + Redis platform shim for the backend labs.'
  spec.description   = 'Ready db and cache handles wired from DATABASE_URL / REDIS_URL ' \
                       'so learners never write connection code. Part of the backend labs.'
  spec.authors       = ['Vishesh Rawal']
  spec.homepage      = 'https://github.com/visheshrwl/labkit-ruby'
  spec.license       = 'Apache-2.0'
  spec.required_ruby_version = '>= 3.0'

  spec.files         = Dir['lib/**/*.rb'] + ['README.md']
  spec.require_paths = ['lib']

  spec.add_dependency 'pg', '~> 1.5'
  spec.add_dependency 'redis', '~> 5.0'

  spec.metadata['source_code_uri'] = spec.homepage
end
