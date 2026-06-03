import com.google.protobuf.gradle.id
import com.google.protobuf.gradle.proto

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.protobuf")
}

android {
    namespace = "com.dronesensor.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.dronesensor.app"
        minSdk = 28
        targetSdk = 34
        // Gen 2.X version scheme — X = versionCode, bumped on each
        // release. The on-device screen renders "Gen 2.${VERSION_CODE}"
        // so the installer can read which build is on the phone at a
        // glance.
        versionCode = 5
        versionName = "Gen 2.5"

        // Provisioning defaults. Today these are baked at build time via
        // gradle properties: ./gradlew installDebug -PgrpcHost=... -Psite=...
        // Future: a QR scanner produces a ProvisioningPayload with the same
        // fields and calls AppConfig setters — see ProvisioningPayload.kt
        // for the canonical shape both transports share.
        val pGrpcHost = (project.findProperty("grpcHost") as String?) ?: "10.0.2.2"
        val pGrpcPort = (project.findProperty("grpcPort") as String?)?.toInt() ?: 50051
        val pTls       = (project.findProperty("tls") as String?)?.toBoolean() ?: false
        val pSite      = (project.findProperty("site") as String?) ?: ""
        val pSiteLabel = (project.findProperty("siteLabel") as String?) ?: ""
        val pDeviceId  = (project.findProperty("deviceId") as String?) ?: ""
        val pJwtAud    = (project.findProperty("jwtAudience") as String?) ?: ""

        buildConfigField("String", "DEFAULT_GRPC_HOST", "\"$pGrpcHost\"")
        buildConfigField("int",    "DEFAULT_GRPC_PORT", "$pGrpcPort")
        buildConfigField("boolean","DEFAULT_TLS",       "$pTls")
        buildConfigField("String", "DEFAULT_SITE",       "\"$pSite\"")
        buildConfigField("String", "DEFAULT_SITE_LABEL", "\"$pSiteLabel\"")
        buildConfigField("String", "DEFAULT_DEVICE_ID",  "\"$pDeviceId\"")
        buildConfigField("String", "DEFAULT_JWT_AUDIENCE","\"$pJwtAud\"")
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    sourceSets {
        getByName("main") {
            java.srcDirs("src/main/kotlin")
            proto {
                srcDir("../../proto")
            }
        }
    }

    packaging {
        resources {
            excludes += setOf(
                "META-INF/INDEX.LIST",
                "META-INF/io.netty.versions.properties",
                "META-INF/DEPENDENCIES"
            )
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("com.google.android.material:material:1.12.0")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    implementation("io.grpc:grpc-okhttp:1.65.1")
    implementation("io.grpc:grpc-protobuf-lite:1.65.1")
    implementation("io.grpc:grpc-stub:1.65.1")
    implementation("io.grpc:grpc-kotlin-stub:1.4.1")
    implementation("com.google.protobuf:protobuf-kotlin-lite:3.25.3")

    compileOnly("javax.annotation:javax.annotation-api:1.3.2")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
}

protobuf {
    protoc {
        artifact = "com.google.protobuf:protoc:3.25.3"
    }
    plugins {
        id("grpc") {
            artifact = "io.grpc:protoc-gen-grpc-java:1.65.1"
        }
        id("grpckt") {
            artifact = "io.grpc:protoc-gen-grpc-kotlin:1.4.1:jdk8@jar"
        }
    }
    generateProtoTasks {
        all().forEach { task ->
            task.builtins {
                id("java") { option("lite") }
                id("kotlin") { option("lite") }
            }
            task.plugins {
                id("grpc") { option("lite") }
                id("grpckt")
            }
        }
    }
}
