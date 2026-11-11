use axum::{routing::get, Json, Router};
use serde_json::{json, Value};

async fn health() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

pub fn router() -> Router {
    Router::new().route("/health", get(health))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::Request};
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    #[tokio::test]
    async fn health_ok() {
        let res = router()
            .oneshot(Request::get("/health").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(res.status(), 200);
        let body = res.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(&body[..], br#"{"status":"ok"}"#);
    }
}
