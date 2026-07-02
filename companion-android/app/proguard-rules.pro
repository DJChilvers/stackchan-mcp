# kotlinx.serialization keeps generated serializers via @Serializable; the
# default Compose/Android rules cover the rest. Add app-specific keeps here
# if release minification is enabled later.
-keepclassmembers class **$$serializer { *; }
-keepclasseswithmembers class * {
    kotlinx.serialization.KSerializer serializer(...);
}
