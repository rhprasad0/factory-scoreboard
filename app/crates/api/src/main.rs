#[tokio::main]
async fn main() -> Result<(), lambda_http::Error> {
    lambda_http::run(api::router()).await
}
