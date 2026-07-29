use crate::AdapterError;
use crate::WireProtocol;
use serde::Deserialize;
use std::collections::HashMap;
use std::fmt;
use std::path::Path;
use url::Url;

#[derive(Clone, Default, Eq, PartialEq)]
pub struct SecretString(String);

impl SecretString {
    pub fn expose_secret(&self) -> &str {
        &self.0
    }

    pub fn is_empty(&self) -> bool {
        self.0.trim().is_empty()
    }
}

impl fmt::Debug for SecretString {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SecretString([REDACTED])")
    }
}

impl<'de> Deserialize<'de> for SecretString {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        String::deserialize(deserializer).map(Self)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProviderConfig {
    pub id: String,
    pub display: String,
    pub protocol: WireProtocol,
    pub model: String,
    pub fast_model: Option<String>,
    pub base_url: Url,
    pub api_key: SecretString,
}

#[derive(Clone, Debug)]
pub struct ProviderRegistry {
    pub version: u64,
    pub active: String,
    providers: HashMap<String, ProviderConfig>,
}

#[derive(Debug, Deserialize)]
struct RawProviderRegistry {
    #[serde(default = "default_version")]
    version: u64,
    active: String,
    providers: HashMap<String, RawProviderConfig>,
}

#[derive(Debug, Deserialize)]
struct RawProviderConfig {
    #[serde(rename = "type")]
    provider_type: String,
    #[serde(default)]
    display: String,
    #[serde(default)]
    api_key: SecretString,
    model: String,
    base_url: String,
    fast_model: Option<String>,
}

impl ProviderRegistry {
    pub fn load(path: &Path) -> Result<Self, AdapterError> {
        let contents = std::fs::read_to_string(path).map_err(|source| AdapterError::ConfigIo {
            path: path.display().to_string(),
            source,
        })?;
        Self::from_json(&contents)
    }

    pub fn from_json(contents: &str) -> Result<Self, AdapterError> {
        let raw: RawProviderRegistry = serde_json::from_str(contents)?;
        let mut providers = HashMap::new();
        let mut unsupported_active_type = None;
        for (id, provider) in raw.providers {
            let Some(protocol) = parse_protocol(&provider.provider_type) else {
                if id == raw.active {
                    unsupported_active_type = Some(provider.provider_type);
                }
                continue;
            };
            let base_url = Url::parse(&provider.base_url).map_err(|error| {
                AdapterError::Config(format!("provider `{id}` has invalid base_url: {error}"))
            })?;
            if provider.model.trim().is_empty() {
                return Err(AdapterError::Config(format!(
                    "provider `{id}` has an empty model"
                )));
            }
            if provider.api_key.is_empty() {
                return Err(AdapterError::Config(format!(
                    "provider `{id}` has an empty api_key"
                )));
            }
            let display = if provider.display.trim().is_empty() {
                id.clone()
            } else {
                provider.display
            };
            providers.insert(
                id.clone(),
                ProviderConfig {
                    id,
                    display,
                    protocol,
                    model: provider.model,
                    fast_model: provider.fast_model,
                    base_url,
                    api_key: provider.api_key,
                },
            );
        }
        if !providers.contains_key(&raw.active) {
            if let Some(provider_type) = unsupported_active_type {
                return Err(AdapterError::Config(format!(
                    "active provider `{}` uses unsupported type `{provider_type}`",
                    raw.active
                )));
            }
            return Err(AdapterError::Config(format!(
                "active provider `{}` does not exist",
                raw.active
            )));
        }
        Ok(Self {
            version: raw.version,
            active: raw.active,
            providers,
        })
    }

    pub fn provider(&self, id: &str) -> Option<&ProviderConfig> {
        self.providers.get(id)
    }

    pub fn active_provider(&self) -> Result<&ProviderConfig, AdapterError> {
        self.providers.get(&self.active).ok_or_else(|| {
            AdapterError::Config(format!(
                "active provider `{}` is missing from the registry",
                self.active
            ))
        })
    }

    pub fn providers(&self) -> impl Iterator<Item = &ProviderConfig> {
        self.providers.values()
    }
}

fn parse_protocol(value: &str) -> Option<WireProtocol> {
    match value.trim().to_ascii_lowercase().as_str() {
        "openai_compatible" | "openai-compatible" | "chat_completions" => {
            Some(WireProtocol::OpenAiCompatible)
        }
        "openai_responses" | "responses" => Some(WireProtocol::OpenAiResponses),
        "anthropic" | "anthropic_messages" => Some(WireProtocol::AnthropicMessages),
        "gemini" | "gemini_native" | "google_gemini" => Some(WireProtocol::GeminiNative),
        _ => None,
    }
}

const fn default_version() -> u64 {
    1
}

#[cfg(test)]
mod tests {
    use super::ProviderRegistry;
    use crate::WireProtocol;

    #[test]
    fn loads_legacy_coomi_provider_without_exposing_its_key_in_debug() {
        let registry = ProviderRegistry::from_json(
            r#"{
                "version": 1,
                "active": "opencode-go",
                "providers": {
                    "opencode-go": {
                        "type": "openai_compatible",
                        "display": "OpenCode Go",
                        "api_key": "secret-value",
                        "model": "deepseek-v4-flash",
                        "base_url": "https://example.test/v1",
                        "fast_model": "deepseek-v4-flash"
                    }
                }
            }"#,
        )
        .expect("load provider registry");
        let provider = registry.active_provider().expect("resolve active provider");
        assert_eq!(provider.protocol, WireProtocol::OpenAiCompatible);
        assert_eq!(provider.model, "deepseek-v4-flash");
        let debug = format!("{registry:?}");
        assert!(!debug.contains("secret-value"));
        assert!(debug.contains("[REDACTED]"));
    }
}
