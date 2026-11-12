# Build stage. The pom is copied and resolved on its own first, so a source-only change reuses
# the cached dependency layer instead of downloading everything again.
FROM maven:3.9-eclipse-temurin-21 AS build

WORKDIR /build
COPY pom.xml ./
RUN mvn -B -q dependency:go-offline

COPY src ./src
# Tests need live Redis and MySQL, so they run in the pipeline's test stage, not here.
RUN mvn -B -q package -DskipTests


# Runtime image: a JRE only, no build tools and no source.
FROM eclipse-temurin:21-jre AS runtime

RUN useradd --system --no-create-home --uid 10001 reservationsvc

WORKDIR /app
COPY --from=build /build/target/reservation-service-*.jar app.jar

USER reservationsvc
EXPOSE 8081

# curl is not in the JRE image, so bash opens the socket itself and checks the status line.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD bash -c 'exec 3<>/dev/tcp/127.0.0.1/8081 && printf "GET /health HTTP/1.0\r\n\r\n" >&3 && read -r line <&3 && [[ $line == *" 200 "* ]]'

# Leaves room for the JVM's own memory inside the container's limit.
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
