# Use the same loader and resolved per-host environment as Kamal's containers.
# Never serialize configuration or secrets to stdout or command arguments.
gem "kamal", "2.12.0"
require "kamal"

begin
  config = Kamal::Configuration.create_from(
    config_file: Pathname.new(ENV.fetch("KAMAL_CONFIG_FILE", "config/deploy.yml")),
    destination: ENV["KAMAL_DESTINATION"].presence,
    version: ENV["KAMAL_VERSION"].presence
  )
  config.roles.select { |role| %w[web job].include?(role.name) }.each do |role|
    role.hosts.each do |host|
      environment = ENV.to_h.slice("PATH", "HOME", "UV_CACHE_DIR").merge(
        role.env(host).to_h.transform_values(&:to_s)
      ).merge("SENTRY_DSN" => "", "PYTHON_DOTENV_DISABLED" => "1")
      abort "Production settings check failed." unless system(
        environment, "uv", "run", "--locked", "python", "manage.py",
        "check_production_config", unsetenv_others: true
      )
    end
  end
rescue StandardError
  abort "Unable to resolve production settings; inspect the Kamal configuration and secret sources."
end
